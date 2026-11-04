import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from hephaestus_runtime import Broker, docker_args, validate_mount


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path/'work'; workspace.mkdir()
    config = {'profiles': {'core': {'image': 'sha256:'+'a'*64}},
              'roots': {'readonly': [str(workspace)], 'writable': [str(workspace)]}}
    request = {'id':'operation1','scope':'owner-project','identity':{'owner':'owner','project':'project'},'profile':'core','cwd':str(workspace),
               'command':['sh','-c','echo hello'], 'mounts':[{'source':str(workspace),'target':'/workspace','writable':True}]}
    return config, request


def test_container_has_no_daemon_or_host_network(runtime):
    config, request = runtime
    args = docker_args(request, config)
    assert args[args.index('--network')+1] == 'none'
    assert args[args.index('--cap-drop')+1] == 'ALL'
    assert '--read-only' in args
    assert '--privileged' not in args
    assert args[args.index('--cgroup-parent')+1].startswith('hephaestus-workloads-op')
    assert not any('docker.sock' in value for value in args)


def test_escape_and_symlink_mounts_rejected(runtime, tmp_path):
    config, request = runtime
    outside = tmp_path/'secret'; outside.write_text('private')
    link = Path(config['roots']['writable'][0])/'link'; link.symlink_to(outside)
    for source in [outside, link]:
        with pytest.raises(ValueError): validate_mount(str(source), '/work', True, config['roots'])
    for target in ['/', '/run', '/var/run/docker.sock', '/sys', '/proc']:
        with pytest.raises(ValueError): validate_mount(request['cwd'], target, True, config['roots'])


def test_missing_scope_unpinned_image_and_excess_memory_fail(runtime):
    config, request = runtime
    for change in [{'scope':''}, {'identity':{}}, {'memory_mib':8192}, {'profile':'missing'}]:
        with pytest.raises(ValueError): docker_args({**request, **change}, config)
    config['profiles']['core']['image'] = 'debian:latest'
    with pytest.raises(ValueError): docker_args(request, config)


def test_repeated_dispatch_never_reexecutes(runtime, tmp_path):
    config, request = runtime
    broker = Broker(tmp_path/'broker', config)
    broker.reserve_identity(request)
    with pytest.raises(ValueError, match='already dispatched'): broker.reserve_identity(request)
    with pytest.raises(ValueError, match='different input'): broker.reserve_identity({**request,'command':['false']})
    broker.update(request['id'], 'succeeded', {'exit_code':0})
    with pytest.raises(ValueError, match='already dispatched'): Broker(tmp_path/'broker', config).reserve_identity(request)


def test_explicit_scoped_tmp_is_not_hidden_by_an_anonymous_tmpfs(runtime):
    config,request=runtime
    assert '--tmpfs' in docker_args(request,config)
    request['mounts'].append({'source':request['cwd'],'target':'/tmp','writable':True})
    assert '--tmpfs' not in docker_args(request,config)
