"""Guarded VM maintenance: previews, reference protection and resumable receipts."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import time
import uuid

from hephaestus_inventory import (ACTIVE, atomic, read, control, lock, resource_path,
                                  discover, active_stores, snapshot)

GIB=1024**3
CONFIRMATION='DELETE VM PROJECT DATA'


def run(args, timeout=30):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError(result.stderr[-1500:] or 'Maintenance command failed')
    return result.stdout


def preview_active(state):
    if state.get('lifecycle')=='pinned' and state.get('desired_state')=='running' and state.get('status')!='failed':return True
    pid=state.get('pid')
    if not pid:return state.get('status') in {'starting','restarting'} and time.time()-state.get('created_at',0)<120
    try:
        text=Path('/proc',str(int(pid)),'stat').read_text();fields=text[text.rfind(')')+2:].split()
        if fields[0] in {'Z','X'}:return False
        observed={'pid':int(pid),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'start_ticks':int(fields[19])}
        if state.get('process_identity'):return observed==state['process_identity']
        boot=int(next(line.split()[1] for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime ')))
        return abs(boot+int(fields[19])/os.sysconf('SC_CLK_TCK')-state['process_started'])<.05
    except (OSError,ValueError,KeyError,IndexError,StopIteration):return False


def references(root, registry):
    """Only explicit retention manifests authorize pruning old release references."""
    root=Path(root);pins=read(control(root)/'retention.json')
    current=Path('/etc/hephaestus/current-release')
    releases=set(pins.get('releases',[]));images=set(pins.get('images',[]));paths=set(pins.get('paths',[]))
    if current.exists() and str(root) in current.read_text():releases.add(current.read_text().strip())
    runtime=Path('/etc/hephaestus/runtime.json')
    if runtime.exists():
        config=read(runtime)
        images.update(v['image'] for v in config.get('profiles',{}).values())
    for release in releases:
        data=read(Path(release)/'release.json')
        images.update(str(data[k]) for k in ('image','languages_image','aider_image') if data.get(k))
        paths.update(data.get('environments',{}).values());paths.update(data.get('browsers',{}).values())
        paths.add(str(Path(release).resolve()))
    runtime_paths=set(paths)
    pinned=[];live=[]
    retired=read(control(root)/'retired-stores.json',{'paths':[]})['paths']
    for store in registry['resources'].values():
        if store['kind']!='store' or store.get('role')=='internal':continue
        if any(store['path']==p or store['path'].startswith(p+'/') for p in retired):continue
        path=resource_path(root,store['path'])
        for file in (path/'previews').glob('*/session.json'):
            try:session=read(file)
            except (OSError,ValueError):live.append(str(file));continue
            if preview_active(session):live.append(str(file))
            if session.get('lifecycle')=='pinned' and session.get('status')!='deleted':
                pinned.append(str(file));paths.update(session.get('cache_roots',[]))
                for cache in session.get('cache_roots',[]):
                    for marker in (Path(cache)/'bindings-v2').glob('*.json'):
                        binding=read(marker)
                        if binding.get('browser_path'):paths.add(binding['browser_path'])
                for k in ('image','runtime_image'):
                    if session.get(k):images.add(session[k])
    return {'releases':sorted(releases),'images':sorted(images),'paths':sorted(paths),'pinned':pinned,'live_previews':live,
            'runtime_paths':sorted(runtime_paths),'retention_configured':bool(pins.get('verified'))}


def docker_inventory(refs):
    images=[]
    rows=run(['docker','image','ls','--no-trunc','--format','{{json .}}'])
    containers=[json.loads(line) for line in run(['docker','ps','-a','--no-trunc','--format','{{json .}}']).splitlines() if line]
    used_ids={}
    for container in containers:
        data=json.loads(run(['docker','container','inspect',container['ID']]))[0]
        used_ids.setdefault(data['Image'],[]).append(container)
    grouped={}
    for line in rows.splitlines():
        row=json.loads(line);grouped.setdefault(row['ID'],[]).append(row)
    for identity,tags in grouped.items():
        row=tags[0];used=used_ids.get(identity,[])
        retained=identity in refs['images']
        protected=retained or not refs['retention_configured'] or bool(used)
        reason=('Waiting for verified runtime and rollback inventory' if not refs['retention_configured'] else
                'Referenced runtime or retained rollback' if retained else
                'Referenced by a container' if used else 'Unreferenced image')
        images.append({'id':identity,'name':row['Repository'],'version':row['Tag'],'reported_size':row['Size'],
                       'tags':[t['Repository']+':'+t['Tag'] for t in tags if t['Repository']!='<none>' and t['Tag']!='<none>'],
                       'containers':len(used),'protected':protected,'reason':reason})
    summary=[json.loads(line) for line in run(['docker','system','df','--format','{{json .}}']).splitlines() if line]
    return {'images':images,'summary':summary,'containers':containers,
            'note':'Docker estimates share image/build-cache layers. They are not additive or guaranteed recoverable bytes.'}


def identity(path):
    st=path.lstat();return [st.st_dev,st.st_ino,st.st_mtime_ns]


def bytes_used(path):
    return int(run(['du','-sx','-B1',str(path)]).split()[0])


def candidates(root, registry, refs, mode='safe', automatic=False, *, measure=True):
    root=Path(root);now=time.time();active=active_stores(root,registry)
    active_ids={a['store'] for a in active};rows=[]
    groups=[r for r in registry['resources'].values() if r['kind']=='test_environment']
    for item in registry['resources'].values():
        path=resource_path(root,item['path'])
        if not path.exists():continue
        if any(item['id']!=g['id'] and path.is_relative_to(root/g['path']) for g in groups):continue
        reason='Retained project or controller data';eligible=False;age=now-path.stat().st_mtime
        if item['kind'] in {'cache','shared_cache'}:
            eligible=True;reason='Reconstructible dependencies'
            if automatic and age<7*86400:eligible=False;reason='Used within the last 7 days'
            if item.get('store') in active_ids:eligible=False;reason='Active operation uses this store'
        elif item['kind']=='test_environment':
            receipt=item.get('archive',{})
            eligible=bool(receipt.get('verified') and receipt.get('sha256') and item.get('completed_at'))
            reason='Evidence archived and verified' if eligible else 'Waiting for verified evidence archive and completion'
            if eligible and receipt.get('fingerprint') != evidence_fingerprint(path):eligible=False;reason='Test evidence changed after archival'
            if automatic and now-item.get('completed_at',now)<86400:eligible=False;reason='Test retention: 24 hours'
            children={r['id'] for r in registry['resources'].values() if (root/r['path']).is_relative_to(path)}
            if children & active_ids:eligible=False;reason='Test operations are active'
            for unit in item.get('services',[]):
                result=subprocess.run(['systemctl','is-active','--quiet',unit])
                if result.returncode==0:eligible=False;reason='Test service must be retired before cleanup'
        elif item['kind']=='release':
            eligible=refs['retention_configured'] and str(path) not in refs['releases'] and item.get('retired') is True
            reason='Obsolete release' if eligible else 'Current, rollback, or unreviewed release'
        elif mode=='reset' and item['kind'] in {'project_data','workspace','application'}:
            eligible=True;reason='Explicit project-data reset'
        if any(Path(p).resolve()==path.resolve() or Path(p).resolve().is_relative_to(path.resolve()) or path.resolve().is_relative_to(Path(p).resolve()) for p in refs['paths']):
            eligible=False;reason='Required by a retained runtime or pinned application'
        if mode=='reset' and item['kind'] in {'project_data','workspace','application'}:
            # Reset permits stopped pinned data, but never installed runtime dependencies.
            if not any(path.resolve()==Path(p).resolve() or Path(p).resolve().is_relative_to(path.resolve()) for p in refs['runtime_paths']):eligible=True
        if item.get('role')=='test' and item['kind']=='store':eligible=False;reason='Retire the enclosing test environment after archival'
        rows.append({**item,'eligible':bool(eligible),'reason':reason,'identity':identity(path),
                     'estimated_bytes':bytes_used(path) if eligible and measure else 0})
    # Unknown roots are shown explicitly and never deletion candidates.
    registered={str(Path(r['path']).parts[0]) for r in rows}
    known={'control','releases','state','projects','applications','runs','caches','containerd','docker','lost+found'}
    for path in root.iterdir():
        if path.name not in known and path.name not in registered:
            rows.append({'id':'unknown:'+path.name,'path':path.name,'kind':'unknown','eligible':False,'reason':'Unrecognized data; preserved','estimated_bytes':0})
    return rows,active


def preview(root, mode='safe', automatic=False):
    if mode not in {'safe','reset'}:raise ValueError('Unknown cleanup mode')
    registry=discover(root);refs=references(root,registry)
    rows,active=candidates(root,registry,refs,mode,automatic)
    docker=docker_inventory(refs)
    blockers=active+([{'status':'Active application preview','path':p} for p in refs['live_previews']])
    for c in docker['containers']:
        if c.get('State')=='running':blockers.append({'status':'Running container','id':c['ID']})
    plan={'version':1,'id':uuid.uuid4().hex,'created':time.time(),'mode':mode,'automatic':automatic,
          'resources':rows,'docker':docker,'blockers':blockers,'references':refs,
          'estimated_reclaimable_bytes':sum(r['estimated_bytes'] for r in rows if r['eligible'])}
    atomic(control(root)/'previews'/ (plan['id']+'.json'),plan)
    return plan


def public_plan(plan):
    return {k:v for k,v in plan.items() if k!='references'}


def _active_under(root,path,registry):
    stores={r['id'] for r in registry['resources'].values() if (Path(root)/r['path']).is_relative_to(path)}
    return any(r['store'] in stores for r in active_stores(root,registry))


def execute(root, op_id):
    """All destructive steps have a durable intent and an inode-bound receipt."""
    root=Path(root);record=control(root)/'operations'/(op_id+'.json');op=read(record)
    if op.get('status') not in {'queued','running'}:return
    claimed=False
    try:
        with lock(root,'cleanup',blocking=False),lock(root,'admission',blocking=False):
            op=read(record)
            if op.get('status') not in {'queued','running'}:return
            claimed=True
            registry=discover(root);refs=references(root,registry)
            if active_stores(root,registry) or refs['live_previews']:
                raise RuntimeError('Wait for active jobs and previews to stop before VM cleanup')
            if any(c.get('State')=='running' for c in docker_inventory(refs)['containers']):
                raise RuntimeError('Running containers are protected; retry cleanup after they stop')
            op.update(status='running',started=time.time());atomic(record,op)
            plan=read(control(root)/'previews'/(op['preview_id']+'.json'))
            if op['mode']=='reset':
                # Fence old job identities even after their operation ledgers disappear.
                retired=read(control(root)/'retired-jobs.json',{'jobs':[],'workspaces':[],'projects':[]})
                for row in plan['resources']:
                    if not row['eligible']:continue
                    if row['kind']=='workspace':
                        retired['workspaces']=sorted(set(retired.get('workspaces',[]))|{Path(row['path']).name})
                    if row['kind']!='project_data':continue
                    store=registry['resources'][row['store']]
                    dbpath=root/store['path']/'operations.sqlite3'
                    if dbpath.exists():
                        with sqlite3.connect(f'file:{dbpath}?mode=ro',uri=True) as db:
                            rows=db.execute('SELECT job_id,payload FROM operations').fetchall()
                            retired['jobs']=sorted(set(retired['jobs'])|{r[0] for r in rows})
                            retired['projects']=sorted(set(retired.get('projects',[]))|{json.loads(r[1]).get('project_id') for r in rows if json.loads(r[1]).get('project_id')})
                atomic(control(root)/'retired-jobs.json',retired)
                op['retired_jobs']=retired['jobs'];atomic(record,op)
            current,_=candidates(root,registry,refs,op['mode'],plan.get('automatic',False),measure=False)
            current={r['id']:r for r in current}
            previous={r['id'] for r in op.get('results',[]) if r['status']=='removed'}
            for row in plan['resources']:
                if not row['eligible'] or row['id'] in previous:continue
                if plan.get('pressure'):
                    disk=shutil.disk_usage(root)
                    if disk.used/disk.total<=.7 and disk.free>=10*GIB:break
                path=resource_path(root,row['path'])
                outcome={'id':row['id'],'path':row['path']}
                try:
                    with ExitStack() as stack:
                        stack.enter_context(lock(root,'resource:'+row['id'],blocking=False))
                        if row['kind'] == 'shared_cache':
                            stack.enter_context(lock(root,'shared-cache-readers',blocking=False))
                        if row['kind'] in {'cache','shared_cache'}:
                            from coder_engine_cache import lease
                            stack.enter_context(lease(path,exclusive=True,blocking=False))
                        if path.exists():
                            resuming=op.get('intent',{}).get('id')==row['id']
                            expected=row['identity'][:2] if resuming else row['identity']
                            actual=identity(path)[:2] if resuming else identity(path)
                            if actual!=expected:raise RuntimeError('Resource changed after preview')
                            valid=current.get(row['id'])
                            if not valid or not valid['eligible']:raise RuntimeError((valid or {}).get('reason','Resource is no longer eligible'))
                            if _active_under(root,path,registry):raise RuntimeError('Operation became active')
                            op['intent']={'id':row['id'],'identity':row['identity']};atomic(record,op)
                            if row['kind']=='test_environment':
                                retired=read(control(root)/'retired-stores.json',{'paths':[]})
                                retired['paths']=sorted(set(retired['paths'])|{row['path']})
                                atomic(control(root)/'retired-stores.json',retired)
                            shutil.rmtree(path)
                            if row['kind']=='test_environment':
                                # The acknowledged copy is durable on HyprChat.
                                for suffix in ('.tar.gz','.tar.pending','.tar.pending.json'):(control(root)/'evidence-outbox'/(row['id']+suffix)).unlink(missing_ok=True)
                        outcome['status']='removed'
                except (OSError,RuntimeError,ValueError) as error:outcome.update(status='skipped',reason=str(error)[:400])
                op.setdefault('results',[]).append(outcome);op.pop('intent',None);atomic(record,op)
            # Docker has its own references. Delete by explicit identities, never raw storage paths.
            if refs['retention_configured']:
                reviewed_containers={c['ID'] for c in plan['docker']['containers'] if c.get('State')!='running' and 'hyprchat.managed=execution' in c.get('Labels','')}
                for container in docker_inventory(refs)['containers']:
                    labels=container.get('Labels','')
                    if container['ID'] in reviewed_containers and container.get('State')!='running' and 'hyprchat.managed=execution' in labels:
                        try:run(['docker','rm',container['ID']]);op.setdefault('docker_removed',[]).append(container['ID']);atomic(record,op)
                        except RuntimeError as error:op.setdefault('warnings',[]).append(str(error))
                reviewed_images={r['id'] for r in plan['docker']['images'] if not r['protected']}
                for row in docker_inventory(refs)['images']:
                    if row['id'] in reviewed_images and not row['protected']:
                        try:
                            for tag in row.get('tags',[]) or [row['id']]:run(['docker','image','rm',tag])
                            op.setdefault('docker_removed',[]).append(row['id']);atomic(record,op)
                        except RuntimeError as error:op.setdefault('warnings',[]).append(str(error))
                # BuildKit only removes unused records; image references remain protected.
                try:
                    args=['docker','builder','prune','-f']
                    if plan.get('automatic'):args+=['--filter','until=168h']
                    run(args,timeout=90)
                except RuntimeError as error:op.setdefault('warnings',[]).append(str(error))
            op.update(status='completed',ended=time.time(),free_after=shutil.disk_usage(root).free)
            op['recovered_bytes']=max(0,op['free_after']-op['free_before']);atomic(record,op)
    except BlockingIOError as error:
        # A timer/retry must not overwrite the receipt of the process holding cleanup.
        if not claimed and op.get('status')=='running':return
        op.update(status='blocked',error='Active workloads or another cleanup hold a storage lease',ended=time.time());atomic(record,op)
    except (RuntimeError,OSError,ValueError) as error:
        op.update(status='blocked',error=str(error) or 'Active workloads or another cleanup hold a storage lease',ended=time.time());atomic(record,op)
    finally:
        from hephaestus_inventory import refresh_scan
        threading.Thread(target=refresh_scan,args=(root,),daemon=True).start()


def start(root, preview_id, operation_id, confirmation='', automatic=False):
    import re
    if not re.fullmatch('[a-f0-9]{32}',preview_id or '') or not re.fullmatch('[a-f0-9]{32}',operation_id or ''):raise ValueError('Invalid maintenance identity')
    path=control(root)/'operations'/(operation_id+'.json')
    with lock(root,'submit'):
        if path.exists():
            op=read(path)
            if op['preview_id']!=preview_id:raise ValueError('Operation identity already belongs to another preview')
            return op
        plan=read(control(root)/'previews'/(preview_id+'.json'))
        if not plan or time.time()-plan['created']>300:raise ValueError('Cleanup preview expired; review a fresh preview')
        if plan['mode']=='reset' and confirmation!=CONFIRMATION:raise ValueError('Type '+CONFIRMATION+' to confirm')
        if plan['mode']=='reset' and plan['blockers']:raise ValueError('Stop active jobs and previews before resetting project data')
        op={'id':operation_id,'preview_id':preview_id,'mode':plan['mode'],'status':'queued',
            'created':time.time(),'free_before':shutil.disk_usage(root).free,'results':[]}
        atomic(path,op)
    threading.Thread(target=execute,args=(root,operation_id),daemon=True).start()
    return op


def recover(root):
    for path in (control(root)/'operations').glob('*.json'):
        op=read(path)
        if op.get('status') in {'queued','running'}:execute(root,op['id'])


def status(root):
    operations=sorted((control(root)/'operations').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:5]
    return {'snapshot':snapshot(root),'operations':[read(p) for p in operations],
            'policy':{'interval_minutes':10,'test_hours':24,'cache_days':7,'pressure_percent':80,'target_percent':70,'reserve_bytes':10*GIB}}


def automatic(root):
    plan=preview(root,automatic=True)
    usage=shutil.disk_usage(root)
    if usage.used/usage.total>=.8 or usage.free<10*GIB:
        plan=preview(root,automatic=False);plan['pressure']=True
        atomic(control(root)/'previews'/(plan['id']+'.json'),plan)
    if not plan['blockers'] and (any(r['eligible'] for r in plan['resources']) or usage.used/usage.total>=.8):
        op_id=uuid.uuid4().hex;record={'id':op_id,'preview_id':plan['id'],'mode':'safe','status':'queued','created':time.time(),'free_before':usage.free,'results':[]}
        atomic(control(root)/'operations'/(op_id+'.json'),record);execute(root,op_id)
    snapshot(root)


EVIDENCE_EXCLUDED={'project-environments','operation-caches','tool-caches','node_modules','vendor','__pycache__','.git','.pytest_cache','browsers'}


def evidence_fingerprint(path):
    digest=hashlib.sha256()
    for directory,dirs,names in os.walk(path,followlinks=False):
        dirs[:]=sorted(name for name in dirs if name not in EVIDENCE_EXCLUDED
            and not (Path(directory)/name).is_symlink() and not (Path(directory)/name/'pyvenv.cfg').exists())
        for name in sorted(names):
            file=Path(directory)/name
            if file.is_symlink() or not file.is_file():continue
            st=file.stat()
            digest.update(json.dumps([str(file.relative_to(path)),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]).encode())
    return digest.hexdigest()


def prepare_archives(root):
    """Produce evidence-only bundles; a separate server receipt permits deletion."""
    from hephaestus_inventory import register
    root=Path(root);registry=discover(root);outbox=control(root)/'evidence-outbox';outbox.mkdir(exist_ok=True)
    records=[]
    groups=[r for r in registry['resources'].values() if r['kind']=='test_environment']
    for row in groups:
        path=resource_path(root,row['path'])
        if not path.exists() or row.get('archive',{}).get('verified') or not row.get('completed_at'):continue
        if any(row['id']!=g['id'] and path.is_relative_to(root/g['path']) for g in groups):continue
        from hephaestus_archive import archive_environment
        idle=lambda:not _active_under(root,path,registry) and not any(
            subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0 for unit in row.get('services',[]))
        record=archive_environment(root,row,path,outbox,idle=idle,fingerprint=evidence_fingerprint,identity=identity,excluded=EVIDENCE_EXCLUDED)
        if record:records.append(record)
    return records


def acknowledge_archive(root, identity_value, sha, destination):
    from hephaestus_inventory import register
    record=read(control(root)/'evidence-outbox'/(identity_value+'.json'))
    if not record or record.get('sha256')!=sha:raise ValueError('Archive checksum does not match')
    if not destination.startswith('/opt/hyprchat/data/evaluation-archives/hephaestus/'):
        raise ValueError('Archive must be retained on the HyprChat server')
    resource=read(control(root)/'registry.json')['resources'][identity_value]
    path=resource_path(root,resource['path'])
    if identity(path)!=record['identity'] or evidence_fingerprint(path)!=record['fingerprint']:raise ValueError('Test environment changed after archival')
    register(root,path,resource['kind'],archive={**record,'verified':True,'destination':destination,'at':time.time()})
    return {'verified':True,'id':identity_value}
