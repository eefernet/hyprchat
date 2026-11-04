"""Explicit retirement gate; never called by provisioning or installer reruns."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

REQUIRED_GATES = {'installer','languages','uploads_files_downloads','legacy_openhands_aider',
                  'modern_opt_in','documents','web_desktop_interactions','pinned_app_recovery',
                  'containment','cancellation_deadlines','worker_vm_restart','revision_preservation',
                  'authenticated_previews','browser_console','post_cutover_health','storage_reserve'}


def digest(path):
    with Path(path).open('rb') as source: return hashlib.file_digest(source,'sha256').hexdigest()


def validate(record, old_config, old_status, new_config):
    if record.get('version')!=1 or record.get('old_guest',{}).get('vmid')!=115:
        raise ValueError('This retirement operation only supports the explicitly recorded Codebox CT115')
    expected=record['old_guest']
    if digest(expected['config_archive'])!=expected['config_sha256']:
        raise ValueError('Archived Codebox identity failed checksum verification')
    if hashlib.sha256(old_config.encode()).hexdigest()!=expected['config_sha256']:
        raise ValueError('CT115 configuration changed; re-inventory it before retirement')
    if '\nhostname: agentcodebox\n' not in '\n'+old_config or 'local-lvm:vm-115-disk-0' not in old_config or 'BC:24:11:8C:87:5C' not in old_config:
        raise ValueError('CT115 no longer matches the verified Codebox identity')
    if old_status.strip()!='status: stopped': raise ValueError('Codebox must remain stopped after successful cutover')
    replacement=record.get('replacement',{})
    if replacement.get('vmid')==115 or new_config.get('name')!='hephaestus' or replacement.get('tag') not in new_config.get('tags','').split(';'):
        raise ValueError('Replacement VM identity does not match the migration record')
    if not record.get('cutover_at') or not record.get('reboot_verified_at') or not record.get('workspaces_marked_unavailable'):
        raise ValueError('Cutover, reboot recovery, and workspace retirement must be completed first')
    if record.get('data_disk_free_bytes',0)<10*1024**3: raise ValueError('Data disk reserve has not been demonstrated')
    evidence=record.get('evidence_archive',{})
    if digest(evidence['path'])!=evidence['sha256']: raise ValueError('Historical evidence archive failed checksum verification')
    reports=record.get('gates',{})
    if set(reports)!=REQUIRED_GATES: raise ValueError('Migration record does not contain every required gate')
    for name,report in reports.items():
        if report.get('passed') is not True or report.get('release')!=replacement.get('release'):
            raise ValueError('Gate is missing or belongs to another release: '+name)
        if digest(report['path'])!=report['sha256']: raise ValueError('Gate evidence changed: '+name)
    if replacement.get('release')!=record.get('post_cutover_release'):
        raise ValueError('Post-cutover source differs from the tested release')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record',type=Path,required=True)
    parser.add_argument('--retire-verified-codebox-ct115',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0: raise SystemExit('Run on pve2 as root')
    record=json.loads(args.record.read_text())
    def output(*command): return subprocess.check_output(command,text=True)
    old_config=output('pct','config','115')
    old_status=output('pct','status','115')
    new_config=json.loads(output('pvesh','get',f'/nodes/{os.uname().nodename}/qemu/{record["replacement"]["vmid"]}/config','--output-format','json'))
    validate(record,old_config,old_status,new_config)
    if not args.retire_verified_codebox_ct115:
        print('All recorded retirement gates passed. No guest was deleted.');return
    # pct destroy removes this guest's configured root disk; do not sweep unrelated volumes.
    subprocess.run(['pct','destroy','115','--purge','1'],check=True)
    record['retired_at']=time.time()
    temporary=args.record.with_suffix('.tmp');temporary.write_text(json.dumps(record,indent=2)+'\n');temporary.chmod(0o600);temporary.replace(args.record)
    print('Retired verified Codebox CT115 and its configured root disk.')


if __name__=='__main__': main()
