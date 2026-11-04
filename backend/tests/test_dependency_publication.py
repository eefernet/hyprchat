import errno
import json
from pathlib import Path
import shutil
from unittest.mock import patch
import pytest
from coder_environment_publish import publish, publication_lock, PreparationFailure


def fixture(tmp_path):
    source=tmp_path/'work/node_modules'; source.mkdir(parents=True)
    (source/'bin').mkdir(); (source/'bin/run').write_text('hello');(source/'bin/run').chmod(0o751)
    (source/'run').symlink_to('bin/run')
    destination=tmp_path/'cache/key/node_modules';destination.parent.mkdir(parents=True)
    return source,destination


def cross_device(source):
    original=Path.rename
    def rename(path,target):
        if path==source and Path(target).name==source.name: raise OSError(errno.EXDEV,'quota boundary')
        return original(path,target)
    return patch.object(Path,'rename',rename)


@pytest.mark.parametrize('cross',[False,True])
def test_publish_preserves_content_modes_links_and_reuses(tmp_path,cross):
    source,destination=fixture(tmp_path)
    with cross_device(source) if cross else patch('os.getpid',return_value=1):
        with publication_lock(destination.parent): publish(source,destination)
    assert source.is_symlink() and source.resolve()==destination
    assert (source/'run').read_text()=='hello'
    assert (destination/'bin/run').stat().st_mode & 0o777==0o751
    publish(source,destination)
    assert not list(destination.parent.glob('.publish-*'))
    assert not list(source.parent.glob('.daedalus-publish-*'))


def test_external_links_rejected_before_moving(tmp_path):
    source,destination=fixture(tmp_path)
    (source/'outside').symlink_to('/etc/passwd')
    with pytest.raises(PreparationFailure,match='outside'):publish(source,destination)
    assert source.is_dir() and not source.is_symlink() and not destination.exists()


def test_absolute_internal_link_is_rebased(tmp_path):
    source,destination=fixture(tmp_path)
    (source/'absolute').symlink_to(source/'bin/run')
    publish(source,destination)
    assert (destination/'absolute').read_text()=='hello'
    assert not (destination/'absolute').readlink().is_absolute()


def test_copy_interrupted_leaves_original_and_no_ready_entry(tmp_path):
    source,destination=fixture(tmp_path)
    def interrupted(src,dst,**kwargs):
        Path(dst).mkdir();(Path(dst)/'partial').write_text('partial')
        raise OSError(errno.ENOSPC,'disk filled')
    with cross_device(source),patch('shutil.copytree',interrupted):
        with pytest.raises(PreparationFailure) as error:publish(source,destination)
    assert error.value.diagnostic['category']=='storage'
    assert not source.is_symlink() and (source/'run').read_text()=='hello'
    assert not destination.exists() and not list(destination.parent.glob('.publish-*'))


def test_preflight_rejects_before_copy(tmp_path):
    source,destination=fixture(tmp_path)
    with cross_device(source),patch('coder_environment_publish.shutil.disk_usage',return_value=shutil._ntuple_diskusage(100,99,1)):
        with pytest.raises(PreparationFailure,match='free space'):publish(source,destination)
    assert not source.is_symlink() and not destination.exists()


def test_interrupted_workspace_switch_recovers(tmp_path):
    source,destination=fixture(tmp_path)
    original=Path.replace
    def interrupted(path,target):
        if path.name.startswith('.daedalus-link-'):raise OSError(errno.EACCES,'switch blocked')
        return original(path,target)
    with cross_device(source),patch.object(Path,'replace',interrupted):
        with pytest.raises(PreparationFailure):publish(source,destination)
    assert (source/'run').read_text()=='hello'
    publish(source,destination)
    assert source.is_symlink() and (source/'run').read_text()=='hello'


def test_completed_cache_never_overwritten(tmp_path):
    source,destination=fixture(tmp_path);publish(source,destination)
    source.unlink();source.mkdir();(source/'other').write_text('different')
    with pytest.raises(PreparationFailure,match='differs'):publish(source,destination)
    assert (source/'other').read_text()=='different'
    assert (destination/'run').read_text()=='hello'


def test_per_key_lock_cleans_only_abandoned_temporary_entries(tmp_path):
    source,destination=fixture(tmp_path)
    stale=destination.parent/'.publish-interrupted';stale.mkdir();(stale/'data').write_text('partial')
    retained=destination.parent/'retained';retained.mkdir()
    with publication_lock(destination.parent):assert not stale.exists() and retained.exists()


def test_browser_failure_never_marks_environment_ready(tmp_path,monkeypatch):
    from .test_engine_reliability import operation
    import coder_project_runtime as runtime
    import coder_engine_environment as environment
    from coder_profiles import environment_key
    from coder_engine_cache import scope
    monkeypatch.delenv('HEPHAESTUS_EXECUTOR',raising=False)
    root=tmp_path/'source';root.mkdir();(root/'package.json').write_text('{}')
    folder=tmp_path/'logs';folder.mkdir();store=operation(tmp_path/'worker')
    profile={'cwd':'.','commands':{'setup':['npm install']},'toolchains':[]}
    def execute(*args,**kw):
        deps=root/'node_modules';deps.mkdir(exist_ok=True);(deps/'library.js').write_text('library')
        return 0
    monkeypatch.setattr(runtime,'execute',execute)
    monkeypatch.setattr(environment,'prepare_browsers',lambda *a:[{'passed':False,'environment_fault':True}])
    rows=runtime.prepare_environment(store,'op',root,profile,'project',{},folder)
    cache=store.root/'project-environments'/scope('project')/environment_key(root,profile)[0]
    assert not all(row['passed'] for row in rows)
    assert not (cache/'environment-ready.json').exists()
    assert not list(cache.glob('[a-f0-9]'*64+'.json'))
    monkeypatch.setattr(environment,'prepare_browsers',lambda *a:[{'passed':True}])
    rows=runtime.prepare_environment(store,'op',root,profile,'project',{},folder)
    from coder_prepared_environment import is_ready
    assert all(row['passed'] for row in rows) and is_ready(cache.parent, root, cache.name, 'op')


def test_persistent_environment_fault_parks_without_code_repair(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from coder_engine_controller import step
    saved=[]
    class Store:
        async def save(self,*args,**kw):saved.append(kw);return kw
        async def record_check(self,*args):pass
    async def operate(*args,**kw):
        return {'checks':[{'passed':False,'environment_fault':True,'diagnostic':{'category':'storage'}}],
            'summary':{'checks_passed':False,'failures':['storage']},'execution_profile':{}},{'worker_operation':'op'}
    controller=SimpleNamespace(store=Store(),_operate=operate)
    asyncio.run(step({'id':'job','state':'checking','revision_id':'rev','repair_round':0,'preparation_retries':1},controller))
    assert saved[-1]['state']=='blocked' and saved[-1]['failure']['stage']=='checking'
    assert 'repair_round' not in saved[-1]
