"""Resumable Proxmox provisioning; retirement is deliberately a separate operation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import io
import tarfile
import urllib.request
import uuid

IMAGE_NAME = 'debian-13-genericcloud-amd64.qcow2'
IMAGE_URL = 'https://cloud.debian.org/images/cloud/trixie/latest/'


def run(*args):
    result = subprocess.run([str(x) for x in args], check=True, text=True, capture_output=True)
    if result.stderr.strip(): print(result.stderr.strip(), file=sys.stderr)
    return result.stdout.strip()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def checksum_entry(text, name):
    matches = [line.split()[0].lower() for line in text.splitlines()
               if len(line.split()) == 2 and line.split()[1].lstrip('*./') == name]
    if len(matches) != 1 or not re.fullmatch(r'[a-f0-9]{128}', matches[0]):
        raise ValueError('Missing or ambiguous official cloud-image checksum')
    return matches[0]


def owned_guest(record, guests):
    guest = next((g for g in guests if int(g['vmid']) == record['vmid']), None)
    if guest and (guest['type'] != 'qemu' or guest.get('name') != 'hephaestus'
                  or record['tag'] not in guest.get('tags', '').split(';')):
        raise ValueError('VMID belongs to another guest; no changes made')
    return guest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vmid', type=int)
    parser.add_argument('--storage', default='local-lvm')
    parser.add_argument('--bridge', default='vmbr0')
    parser.add_argument('--nameserver', default=os.environ.get('HEPHAESTUS_NAMESERVER', '1.1.1.1'), help='Guest resolver handed to cloud-init')
    parser.add_argument('--ssh-public-key', type=Path)
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/hephaestus-installer'))
    parser.add_argument('--plan', action='store_true', help='Read-only inventory; do not create files or guests')
    parser.add_argument('--provision-only', action='store_true', help='Create the guest without installing its runtime')
    parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    guests = json.loads(run('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'))
    path = args.state_dir/'provision.json'
    record = json.loads(path.read_text()) if path.exists() else None
    if record:
        if args.vmid is not None and args.vmid != record['vmid']:
            raise ValueError('This installation already has a different VMID')
        guest = owned_guest(record, guests)
        if not guest and record.get('created'):
            raise ValueError('Previously created VM is missing; refusing to recreate it automatically')
    else:
        identity = args.vmid or int(run('pvesh', 'get', '/cluster/nextid'))
        if identity < 100 or any(int(g['vmid']) == identity for g in guests):
            raise ValueError('VMID is occupied or invalid; choose a free ID')
        record = {'version': 1, 'vmid': identity, 'tag': 'hephaestus-'+uuid.uuid4().hex[:12],
                  'storage': args.storage, 'bridge': args.bridge}
        guest = None
    if args.plan:
        print(json.dumps({**record, 'name': 'hephaestus', 'memory_mib': 8192, 'cores': 4,
                          'system_gib': 20, 'data_gib': 80, 'network': 'dhcp'}, indent=2))
        return
    if os.geteuid() != 0: raise ValueError('Run as root on the Proxmox host')
    if not args.provision_only and not (args.source_root/'backend/engine-runtime.lock.txt').exists():
        raise ValueError('Provide the complete source checkout with --source-root, or choose --provision-only')
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Persist ownership before creating anything, including a partially created VM.
    save(path, record)
    public_key = args.ssh_public_key or args.state_dir/'id_ed25519.pub'
    if args.ssh_public_key is None and not public_key.exists():
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(public_key)[:-4], '-C', 'hephaestus-installer')
    key = public_key.read_text().strip()
    if not key.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')):
        raise ValueError('Provide an OpenSSH public key')
    vmid = record['vmid']
    if not guest:
        if 'image_sha512' not in record:
            with urllib.request.urlopen(IMAGE_URL+'SHA512SUMS', timeout=60) as response:
                record['image_sha512'] = checksum_entry(response.read().decode(), IMAGE_NAME)
            record['image_url'] = IMAGE_URL+IMAGE_NAME
            save(path, record)
        image = args.state_dir/IMAGE_NAME
        if not image.exists():
            run('curl', '--fail', '--location', '--retry', '3', '--output', str(image)+'.part', record['image_url'])
            Path(str(image)+'.part').replace(image)
        with image.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha512').hexdigest()
        if digest != record['image_sha512']:
            raise ValueError('Cloud-image checksum mismatch; no VM created. Remove the invalid cached image to retry.')
        run('qm', 'create', vmid, '--name', 'hephaestus', '--tags', record['tag'], '--memory', '8192',
            '--balloon', '0', '--cores', '4', '--cpu', 'host', '--scsihw', 'virtio-scsi-single',
            '--net0', f"virtio,bridge={record['bridge']}", '--agent', 'enabled=1', '--onboot', '1',
            '--serial0', 'socket', '--vga', 'serial0')
        record['created'] = True; save(path, record)
    config = json.loads(run('pvesh', 'get', f'/nodes/{os.uname().nodename}/qemu/{vmid}/config', '--output-format', 'json'))
    if record['tag'] not in config.get('tags', '').split(';'):
        raise ValueError('Guest identity changed; refusing to configure it')
    for slot in ('scsi0','scsi1'):
        if slot in config and not re.match(re.escape(record['storage'])+r':vm-'+str(vmid)+r'-disk-\d+(?:,|$)',config[slot]):
            raise ValueError('Unexpected disk identity in '+slot+'; no disk was changed')
    if any(re.fullmatch(r'(?:scsi|virtio|sata)\d+',slot) and slot not in {'scsi0','scsi1'} for slot in config):
        raise ValueError('Unexpected additional disk; review the VM configuration before resuming')
    if 'scsi0' not in config:
        run('qm', 'set', vmid, '--scsi0', f"{record['storage']}:0,import-from={args.state_dir/IMAGE_NAME},discard=on")
    existing_size=re.search(r'(?:^|,)size=(\d+)G(?:,|$)',config.get('scsi0',''))
    if not existing_size or int(existing_size.group(1))<20:
        run('qm', 'resize', vmid, 'scsi0', '20G')
    if 'scsi1' not in config:
        run('qm', 'set', vmid, '--scsi1', f"{record['storage']}:80,serial=HEPHAESTUS_DATA,discard=on")
    elif 'serial=HEPHAESTUS_DATA' not in config['scsi1']:
        raise ValueError('Unexpected data disk; refusing to replace it')
    if 'ide2' not in config:
        run('qm', 'set', vmid, '--ide2', f"{record['storage']}:cloudinit")
    if not record.get('configured'):
        # Cloud images include cloud-init; the guest bootstrap installs the guest agent over SSH.
        run('qm', 'set', vmid, '--boot', 'order=scsi0', '--ciuser', 'root', '--sshkeys', public_key,
            '--ipconfig0', 'ip=dhcp', '--nameserver', args.nameserver)
        record['configured'] = True; save(path, record)
    if 'running' not in run('qm', 'status', vmid): run('qm', 'start', vmid)
    if not args.provision_only:
        private_key=Path(str(public_key)[:-4])
        if not private_key.is_file(): raise ValueError('Runtime setup needs the matching SSH private key on this host')
        current=json.loads(run('pvesh','get',f'/nodes/{os.uname().nodename}/qemu/{vmid}/config','--output-format','json'))
        mac=re.search(r'(?:virtio|e1000)=([A-Fa-f0-9:]{17})',current['net0']).group(1)
        octets=[int(value,16) for value in mac.split(':')];octets[0]^=2
        expanded=[*octets[:3],255,254,*octets[3:]]
        local='fe80::'+':'.join(f'{expanded[i]*256+expanded[i+1]:x}' for i in range(0,8,2))+'%'+record['bridge']
        ssh=['ssh','-i',str(private_key),'-o','BatchMode=yes','-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=5','root@'+local]
        until=time.monotonic()+300
        while subprocess.run([*ssh,'true'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            if time.monotonic()>until: raise ValueError('Guest SSH did not become ready; rerun to resume setup')
            time.sleep(3)
        address=json.loads(run(*ssh,'ip -4 -j address show scope global'))
        record['staging_ip']=next(a['local'] for interface in address for a in interface.get('addr_info',[]) if a['family']=='inet')
        save(path,record)
        bundle=io.BytesIO()
        with tarfile.open(fileobj=bundle,mode='w:gz') as archive:
            for parent in ('backend','scripts/hephaestus'):
                for source in (args.source_root/parent).rglob('*'):
                    if source.is_file() and '__pycache__' not in source.parts and source.suffix in {'.py','.txt','.json','.sh','.core'}:
                        archive.add(source,arcname=str(source.relative_to(args.source_root)))
        subprocess.run([*ssh,'install -d -m 700 /root/hephaestus-source && tar xzf - -C /root/hephaestus-source'],input=bundle.getvalue(),check=True)
        subprocess.run([*ssh,'bash /root/hephaestus-source/scripts/hephaestus/bootstrap.sh --initialize-new-data-disk && python3 /root/hephaestus-source/scripts/hephaestus/install.py'],check=True)
        record['runtime_staged']=True;save(path,record)
    print(json.dumps({**record, 'public_key': str(public_key),
                      'next': 'Run compatibility and migration checks before transferring the Codebox address. Codebox is unchanged.'}, indent=2))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr); sys.exit(1)
