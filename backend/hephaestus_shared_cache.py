"""Public, integrity-checked downloads; project scripts never write shared entries."""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

from hephaestus_inventory import atomic, lock, managed_root, read, register

PUBLIC={'registry.npmjs.org','pypi.org','files.pythonhosted.org'}


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed=urllib.parse.urlsplit(newurl)
        if parsed.hostname not in PUBLIC or parsed.scheme!='https' or parsed.username or parsed.password or parsed.port:
            raise ValueError('Shared cache download left the public registry')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch(url, maximum=64*1024**2):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname not in PUBLIC or parsed.username or parsed.password or parsed.port:
        raise ValueError('Only public registry downloads may be shared')
    opener=urllib.request.build_opener(PublicRedirect())
    with opener.open(urllib.request.Request(url,headers={'User-Agent':'HyprChat-public-cache/1'}),timeout=15) as response:
        body=response.read(maximum+1)
    if len(body)>maximum:raise ValueError('Shared download exceeds its limit')
    return body


def download(root, url, digest, algorithm='sha256', filename=None):
    if algorithm not in {'sha256','sha512'} or not re.fullmatch('[a-f0-9]+',digest):raise ValueError('Expected registry integrity digest')
    key=hashlib.sha256((algorithm+':'+digest).encode()).hexdigest()
    entry=Path(root)/'caches/shared/packages'/key
    filename=filename or Path(urllib.parse.urlsplit(url).path).name
    if Path(filename).name!=filename or not filename:raise ValueError('Invalid package filename')
    from hephaestus_storage import ensure_quota
    if (Path(root)/'control/quotas/registry.json').exists():ensure_quota(root,entry.parent,5*1024**3)
    with lock(root,'package:'+key):
        target=entry/filename
        if target.is_file() and hashlib.new(algorithm,target.read_bytes()).hexdigest()==digest:
            os.utime(entry,None);return target,True
        body=fetch(url)
        if hashlib.new(algorithm,body).hexdigest()!=digest:raise ValueError('Public package integrity check failed')
        from coder_environment_publish import require_capacity
        entry.parent.mkdir(parents=True,exist_ok=True);require_capacity(entry.parent,len(body))
        temporary=Path(tempfile.mkdtemp(prefix='.publish-',dir=entry.parent))
        try:
            (temporary/filename).write_bytes(body)
            atomic(temporary/'ready.json',{'url':url,'digest':digest,'algorithm':algorithm,'filename':filename})
            if entry.exists():
                from coder_engine_cache import lease
                with lock(root,'shared-cache-readers',blocking=False),lease(entry,exclusive=True,blocking=False):shutil.rmtree(entry)
            temporary.rename(entry)
        finally:
            if temporary.exists():shutil.rmtree(temporary)
        register(root,entry,'shared_cache')
        return entry/filename,False


def project_downloads(cwd, root, *, limit=12):
    """Bounded prefetch. Unpinned/private/source-built dependencies stay private."""
    cwd=Path(cwd);downloads=[];started=time.monotonic()
    lockfile=cwd/'package-lock.json'
    if lockfile.is_file():
        packages=json.loads(lockfile.read_text()).get('packages',{})
        for package in packages.values():
            if len(downloads)>=limit or time.monotonic()-started>30:break
            url=package.get('resolved','');integrity=package.get('integrity','')
            if not url.startswith('https://registry.npmjs.org/') or not integrity.startswith('sha512-'):continue
            digest=base64.b64decode(integrity.split('-',1)[1]).hex()
            path,reused=download(root,url,digest,'sha512')
            downloads.append({'kind':'node','path':str(path),'reused':reused})
    requirements=cwd/'requirements.txt'
    if requirements.is_file():
        text=requirements.read_text()
        if any(line.strip() and not line.lstrip().startswith('#') and not re.fullmatch(r'\s*[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+\s*(?:#.*)?',line) for line in text.splitlines()):
            return downloads
        for line in requirements.read_text().splitlines():
            if len(downloads)>=limit or time.monotonic()-started>30:break
            match=re.fullmatch(r'\s*([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s*(?:#.*)?',line)
            if not match:continue
            name,version=match.groups()
            metadata=json.loads(fetch('https://pypi.org/pypi/'+urllib.parse.quote(name)+'/'+urllib.parse.quote(version)+'/json',2*1024**2))
            # Universal wheels are compatible across supported CPython runtimes.
            for row in metadata.get('urls',[]):
                if row['filename'].endswith(('py3-none-any.whl','py2.py3-none-any.whl')) and not row.get('yanked'):
                    path,reused=download(root,row['url'],row['digests']['sha256'],filename=row['filename'])
                    downloads.append({'kind':'python','path':str(path),'reused':reused});break
    return downloads


def browser(root, identity, runtime_image, build):
    """Install the independently verified official driver in a source-free sandbox."""
    version=identity['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Expected an exact Playwright version')
    key=hashlib.sha256(json.dumps({'version':version,'identity':identity,
        'image':runtime_image,'machine':platform.machine()},sort_keys=True).encode()).hexdigest()
    entry=Path(root)/'caches/shared/browsers'/key
    from coder_engine_environment import valid_browser_marker, browser_marker
    from hephaestus_storage import ensure_quota
    if (Path(root)/'control/quotas/registry.json').exists():ensure_quota(root,entry.parent,5*1024**3)
    with lock(root,'browser:'+key):
        if valid_browser_marker(entry,identity):os.utime(entry,None);return entry,True
        metadata=json.loads(fetch('https://registry.npmjs.org/playwright-core/'+version,4*1024**2))
        integrity=metadata['dist']['integrity']
        if not integrity.startswith('sha512-'):raise ValueError('Official driver integrity is unavailable')
        package,_=download(root,metadata['dist']['tarball'],base64.b64decode(integrity[7:]).hex(),'sha512')
        staging=Path(root)/'runs'/('browser-publisher-'+key[:20])
        if staging.exists():shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(package) as archive:
                for member in archive.getmembers():
                    relative=Path(member.name)
                    if relative.is_absolute() or '..' in relative.parts or not (member.isfile() or member.isdir()):raise ValueError('Unsafe official driver archive')
                archive.extractall(staging,filter='data')
            driver=staging/'package'
            if hashlib.sha256((driver/'browsers.json').read_bytes()).hexdigest()!=identity['browser_manifest']:
                raise ValueError('Project browser metadata differs from the official driver')
            installed=staging/'browsers';installed.mkdir()
            build(driver,installed)
            executables=[]
            for file in installed.rglob('*'):
                if file.name in {'chrome','chrome-headless-shell','Chromium'} and file.is_file() and os.access(file,os.X_OK):
                    st=file.stat();executables.append({'path':str(file.relative_to(installed)),'stat':[st.st_size,st.st_mtime_ns]})
            if not executables:raise ValueError('Browser install produced no executable')
            atomic(browser_marker(installed,identity),{'version':1,'identity':identity,'executables':executables})
            # A whole new generation is published only after trusted installation completes.
            from coder_environment_publish import publish
            entry.parent.mkdir(parents=True,exist_ok=True)
            if entry.exists():
                # Callers keep existing cache leases; repair never overwrites a live entry.
                from coder_engine_cache import lease
                with lock(root,'shared-cache-readers',blocking=False),lease(entry,exclusive=True,blocking=False):shutil.rmtree(entry)
            publish(installed,entry)
            register(root,entry,'shared_cache')
            return entry,False
        finally:shutil.rmtree(staging,ignore_errors=True)


def published_browser(path):
    root=managed_root(path)
    if root is None or os.environ.get('HEPHAESTUS_SHARED_CACHE')!='1':return False
    path=Path(path)
    parent=root/'caches/shared/browsers'
    if path.parent!=parent or path.is_symlink() or not re.fullmatch('[a-f0-9]{64}',path.name):return False
    from hephaestus_inventory import control
    registry=read(control(root)/'registry.json',{'resources':{}})
    return any(row['kind']=='shared_cache' and row['path']==str(path.relative_to(root)) for row in registry['resources'].values())
