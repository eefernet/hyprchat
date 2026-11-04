import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('hephaestus_provision', Path(__file__).parents[1]/'hephaestus/provision.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def test_checksum_must_identify_exact_image():
    checksum = 'a'*128
    assert p.checksum_entry(checksum+'  image.qcow2\n', 'image.qcow2') == checksum
    for value in (checksum+'  other.qcow2', 'invalid  image.qcow2', (checksum+'  image.qcow2\n')*2):
        with pytest.raises(ValueError): p.checksum_entry(value, 'image.qcow2')


def test_never_adopt_unrelated_guest_or_container():
    record = {'vmid': 118, 'tag': 'hephaestus-123'}
    guest = {'vmid': 118, 'type': 'qemu', 'name': 'hephaestus', 'tags': 'hephaestus-123'}
    assert p.owned_guest(record, [guest]) == guest
    for change in ({'type': 'lxc'}, {'name': 'other'}, {'tags': 'hephaestus-456'}):
        with pytest.raises(ValueError): p.owned_guest(record, [{**guest, **change}])


def test_read_only_plan_does_not_create_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(p, 'run', lambda *args: '[]' if '/cluster/resources' in args else '118')
    state = tmp_path/'absent'
    p.main(['--plan', '--state-dir', str(state)])
    assert not state.exists()
    assert '8192' in capsys.readouterr().out


def test_requested_occupied_id_fails_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'run', lambda *args: '[{"vmid":118,"type":"lxc"}]')
    with pytest.raises(ValueError, match='occupied'):
        p.main(['--vmid', '118', '--state-dir', str(tmp_path/'absent')])
    assert not (tmp_path/'absent').exists()


def test_resume_preserves_existing_disks_and_does_not_recreate(tmp_path,monkeypatch):
    import json
    record={'vmid':118,'tag':'hephaestus-test','created':True,'configured':True,'storage':'local-lvm','bridge':'vmbr0'}
    (tmp_path/'provision.json').write_text(json.dumps(record))
    (tmp_path/'id_ed25519.pub').write_text('ssh-ed25519 valid-test-key')
    guest={'vmid':118,'type':'qemu','name':'hephaestus','tags':'hephaestus-test'}
    config={'tags':'hephaestus-test','scsi0':'local-lvm:vm-118-disk-0,size=20G','scsi1':'local-lvm:vm-118-disk-1,size=80G,serial=HEPHAESTUS_DATA','ide2':'local-lvm:vm-118-cloudinit'}
    calls=[]
    def run(*args):
        calls.append(args)
        if '/cluster/resources' in args:return json.dumps([guest])
        if args[:2]==('qm','status'):return 'status: running'
        if '/nodes/' in str(args):return json.dumps(config)
        raise AssertionError(args)
    monkeypatch.setattr(p,'run',run);monkeypatch.setattr(p.os,'geteuid',lambda:0)
    p.main(['--state-dir',str(tmp_path),'--provision-only'])
    assert not any(args[:2] in {('qm','create'),('qm','resize'),('qm','set')} for args in calls)
    config['scsi1']='local-lvm:vm-999-disk-0,size=80G,serial=HEPHAESTUS_DATA'
    with pytest.raises(ValueError,match='Unexpected disk'):
        p.main(['--state-dir',str(tmp_path),'--provision-only'])
