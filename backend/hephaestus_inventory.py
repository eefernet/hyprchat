"""Controller-owned storage identities and deduplicated XFS capacity snapshots."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import subprocess
import threading
import time

CATEGORIES = {'docker':'Docker images and layers','dependencies':'Dependencies and browsers',
              'projects':'Projects and application data','tests':'Test environments',
              'runtime':'Runtime tools and releases','system':'Shared and system data'}
PRODUCTION={'state/modern','state/legacy','state/documents','state/generic'}

def store_role(relative):
    value=str(relative)
    return 'production' if value in PRODUCTION else 'internal' if any(value.startswith(p+'/') for p in PRODUCTION) else 'unknown'

ACTIVE = {'queued','starting','running','cancelling','stop_unconfirmed'}


def atomic(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+str(os.getpid())+'.'+str(threading.get_ident())+'.pending')
    with tmp.open('w') as handle:
        json.dump(data,handle);handle.flush();os.fsync(handle.fileno())
    tmp.chmod(0o600);tmp.replace(path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def read(path, default=None):
    try:return json.loads(Path(path).read_text())
    except FileNotFoundError:return {} if default is None else default


def control(root):return Path(root)/'control/storage'


@contextmanager
def lock(root, name, *, exclusive=True, blocking=True):
    directory=control(root)/'locks';directory.mkdir(parents=True,exist_ok=True)
    with (directory/(hashlib.sha256(name.encode()).hexdigest()+'.lock')).open('a') as handle:
        fcntl.flock(handle,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|(0 if blocking else fcntl.LOCK_NB))
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def managed_root(path):
    root=Path(os.environ.get('HEPHAESTUS_ROOT','/srv/hephaestus')).resolve()
    return root if Path(path).resolve().is_relative_to(root) and root.exists() else None


def resource_path(root, relative):
    root=Path(root).resolve();path=root/relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts or path==root:
        raise ValueError('Invalid managed resource')
    if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('Managed resource escapes its root')
    return path


def register(root, path, kind, **metadata):
    root=Path(root).resolve();path=Path(path).absolute()
    relative=str(path.relative_to(root));resource_path(root,relative)
    identity=hashlib.sha256((kind+':'+relative).encode()).hexdigest()[:32]
    with lock(root,'registry'):
        registry=read(control(root)/'registry.json',{'version':1,'resources':{}})
        old=registry['resources'].get(identity,{})
        row={**old,'id':identity,'path':relative,'kind':kind,'created':old.get('created',time.time()),**metadata}
        registry['resources'][identity]=row;atomic(control(root)/'registry.json',registry)
    return row


def register_store(path):
    root=managed_root(path)
    if root:
        relative=Path(path).resolve().relative_to(root)
        role=store_role(relative)
        if role=='unknown' and os.environ.get('HEPHAESTUS_STORE_ROLE')=='test':role='test'
        register(root,Path(path),'store',**({'role':role} if role!='unknown' else {}))


def discover(root):
    """Recognize database-backed stores; never infer arbitrary directories are garbage."""
    root=Path(root)
    databases=[]
    for directory,dirs,files in os.walk(root/'state',followlinks=False):
        # Store containers can be nested; their project/dependency contents cannot contain controller ledgers.
        dirs[:]=[d for d in dirs if d not in {'jobs','execution','checks','previews','artifacts','project-environments','operation-caches','node_modules','.venv','vendor','tool-caches','browsers','__pycache__','cache-leases','agent-runtime','legacy-isolated'}
                 and not (Path(directory)/d).is_symlink() and len(Path(directory).relative_to(root).parts)<6]
        if 'operations.sqlite3' in files:databases.append(Path(directory)/'operations.sqlite3')
    for db in databases:
        if db.is_symlink() or not db.resolve().is_relative_to(root.resolve()):continue
        role=store_role(db.parent.relative_to(root))
        store=register(root,db.parent,'store',**({'role':role} if role!='unknown' else {}))
        if store.get('role')=='test':
            try:
                with sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=2) as connection:
                    rows=connection.execute('SELECT status,ended FROM operations').fetchall()
                if rows and all(status not in ACTIVE and ended for status,ended in rows):
                    register(root,db.parent,'test_environment',role='test',completed_at=max(ended for _,ended in rows))
            except sqlite3.Error:pass
    for name in PRODUCTION:
        if (root/name).is_dir():register(root,root/name,'store',role='production')
    # Internal SDK bookkeeping is owned by the parent store, never an independent test environment.
    with lock(root,'registry'):
        registry=read(control(root)/'registry.json',{'version':1,'resources':{}})
        registry['resources']={key:row for key,row in registry['resources'].items()
            if not (row['kind'] in {'store','test_environment'} and any(p in {'agent-runtime','legacy-isolated'} for p in Path(row['path']).parts))}
        atomic(control(root)/'registry.json',registry)
    for store in list(registry['resources'].values()):
        if store['kind']!='store' or store.get('role') not in {'production','test'}:continue
        parent=resource_path(root,store['path'])
        if store.get('role') == 'production':
            # Preserve controller ledgers, events and diagnostics when project data is reset.
            for name in ('jobs','execution','checks','previews','artifacts','agent-runtime','legacy-isolated'):
                path=parent/name
                if path.is_dir() and not path.is_symlink():register(root,path,'project_data',store=store['id'])
        for category in ('project-environments','operation-caches'):
            for path in (parent/category).glob('*'):
                if path.is_dir() and not path.is_symlink():register(root,path,'cache',store=store['id'])
    for path in (root/'caches/shared').glob('*/*'):
        if path.is_dir() and not path.is_symlink():register(root,path,'shared_cache')
    for path in (root/'releases').glob('*'):
        if (path/'release.json').is_file() and not path.is_symlink():register(root,path,'release')
    for path in (root/'projects').glob('*'):
        if path.is_dir() and not path.is_symlink():register(root,path,'workspace')
    for path in (root/'applications').glob('*'):
        if path.is_dir() and not path.is_symlink():register(root,path,'application')
    return read(control(root)/'registry.json')


def active_stores(root, registry):
    active=[]
    retired=read(control(root)/'retired-stores.json',{'paths':[]})['paths']
    for row in registry['resources'].values():
        if row['kind']!='store' or row.get('role')=='internal':continue
        if any(row['path']==p or row['path'].startswith(p+'/') for p in retired):continue
        path=resource_path(root,row['path'])/'operations.sqlite3'
        if not path.exists():continue
        try:
            with sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=2) as db:
                rows=db.execute('SELECT id,status FROM operations').fetchall()
            active.extend({'store':row['id'],'operation':i,'status':s} for i,s in rows if s in ACTIVE)
        except sqlite3.Error:active.append({'store':row['id'],'status':'unreadable operation ledger'})
    # Documents have their own durable status files, outside WorkerStore.
    for path in (Path(root)/'projects').glob('*/root/hyprchat-documents/jobs/*/status.json'):
        try:
            if read(path).get('status') in {'queued','running','starting','cancelling'}:
                active.append({'store':'documents','operation':str(path.relative_to(root)),'status':'Active document job'})
        except (OSError,ValueError):active.append({'store':'documents','status':'Unreadable document status'})
    return active


def category(path, test_roots):
    relative=path.as_posix()
    if any(relative==p or relative.startswith(p+'/') for p in test_roots):return 'tests'
    if path.parts[0] in {'docker','containerd'}:return 'docker'
    if path.parts[0]=='caches' or any(p in {'project-environments','operation-caches','browsers'} for p in path.parts):return 'dependencies'
    if path.parts[0] in {'releases','control'}:return 'runtime'
    if path.parts[0] in {'state','projects','applications','runs'}:return 'projects'
    return 'system'


def extents(fd):
    """FIEMAP returns allocation metadata only; never read raw block devices."""
    start=0
    while True:
        buffer=bytearray(32+56*128)
        struct.pack_into('QQIIII',buffer,0,start,2**64-1-start,0,0,128,0)
        fcntl.ioctl(fd,0xC020660B,buffer,True)
        count=struct.unpack_from('I',buffer,20)[0]
        if not count:return
        for i in range(count):
            logical,physical,length=struct.unpack_from('QQQ',buffer,32+56*i)
            flags=struct.unpack_from('I',buffer,32+56*i+40)[0]
            # Unknown/delayed/inline extents cannot be assigned physical ownership.
            if not flags & (0x2|0x4|0x200|0x100):yield physical,physical+length
            start=logical+length
        if flags & 1:return


def union_counts(intervals):
    """Blocks in different categories belong to the shared/system segment once."""
    events=[]
    for begin,end,kind in intervals:
        events.append((begin,kind,1));events.append((end,kind,-1))
    events.sort();counts={k:0 for k in CATEGORIES};active={};previous=0
    for position,kind,delta in events:
        owners=[k for k,v in active.items() if v]
        if owners:counts[owners[0] if len(owners)==1 else 'system']+=position-previous
        active[kind]=active.get(kind,0)+delta;previous=position
    return counts


def physical_scan(root, registry, *, timeout=180):
    root=Path(root);started=time.monotonic();device=root.stat().st_dev
    tests=[r['path'] for r in registry['resources'].values() if r.get('role')=='test']
    intervals=[];seen={};logical={k:0 for k in CATEGORIES};unknown=[]
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink() and (Path(directory)/d).stat().st_dev==device]
        for name in files:
            if time.monotonic()-started>timeout:raise TimeoutError('Storage scan exceeded its deadline; retaining the previous snapshot')
            path=Path(directory)/name
            try:
                before=path.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_dev!=device:continue
                kind=category(path.relative_to(root),tests);identity=(before.st_dev,before.st_ino)
                logical[kind]+=before.st_blocks*512
                if identity not in seen:
                    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                    try:
                        opened=os.fstat(fd)
                        if (opened.st_dev,opened.st_ino)!=identity:raise RuntimeError('File changed during storage scan')
                        spans=list(extents(fd))
                    finally:os.close(fd)
                    after=path.lstat()
                    if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise RuntimeError('Files changed during storage scan')
                    seen[identity]=spans
                intervals.extend((a,b,kind) for a,b in seen[identity])
            except FileNotFoundError:raise RuntimeError('Files changed during storage scan')
            except OSError as error:
                if len(unknown)<20:unknown.append({'path':str(path.relative_to(root)),'reason':str(error)[:160]})
    import shutil
    usage=shutil.disk_usage(root);counts=union_counts(intervals)
    if sum(counts.values())>usage.used:raise RuntimeError('Storage changed during scan; totals could not be reconciled')
    counts['system']+=usage.used-sum(counts.values())
    return {'version':1,'scanned_at':time.time(),'disk':dict(zip(('total','used','free'),usage)),
            'categories':[{'id':k,'label':v,'bytes':counts[k],'reported_bytes':logical[k]} for k,v in CATEGORIES.items()],
            'unattributed':unknown,'duration':time.monotonic()-started}


def refresh_scan(root):
    try:
        with lock(root,'scan',blocking=False):
            registry=discover(root)
            try:
                snapshot=physical_scan(root,registry)
                from hephaestus_maintenance import references,candidates,docker_inventory
                refs=references(root,registry)
                resources,_=candidates(root,registry,refs)
                tests=[r['path'] for r in registry['resources'].values() if r.get('role')=='test']
                for row in resources:row['category']=category(Path(row['path']),tests)
                snapshot.update(resources=resources,estimated_reclaimable_bytes=sum(r['estimated_bytes'] for r in resources if r['eligible']),docker=docker_inventory(refs))
                atomic(control(root)/'snapshot.json',snapshot);atomic(control(root)/'scan-status.json',{'status':'ok'})
            except Exception as error:atomic(control(root)/'scan-status.json',{'status':'stale','reason':str(error)[:300]})
    except BlockingIOError:pass


def snapshot(root):
    saved=read(control(root)/'snapshot.json')
    if time.time()-saved.get('scanned_at',0)>300:
        threading.Thread(target=refresh_scan,args=(root,),daemon=True).start()
    return {**saved,**read(control(root)/'scan-status.json',{'status':'scanning'})}
