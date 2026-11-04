"""Mark deliberately discarded worker workspaces without deleting history or artifacts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def mark(database, registry):
    database,registry=Path(database),Path(registry)
    if registry.exists(): return json.loads(registry.read_text())
    backup=registry.with_suffix('.before.sqlite3')
    if not backup.exists():
        with sqlite3.connect(database) as source,sqlite3.connect(backup) as target: source.backup(target)
    backup.chmod(0o600)
    with sqlite3.connect(database) as connection:
        connection.row_factory=sqlite3.Row
        connection.execute('BEGIN IMMEDIATE')
        active={'queued','inspecting','baselining','planning','verifying','coding','checking','reviewing','auditing','visual_review','accepting','packaging','candidate_packaging','cancelling','building','fixing','running'}
        rows=connection.execute('SELECT id,state,job_json,workflow_version FROM coder_workflows').fetchall()
        if any(row['state'] in active for row in rows): raise ValueError('Drain coding workflows before retiring their workspaces')
        if connection.execute("SELECT 1 FROM runs WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone():
            raise ValueError('Drain execution and document runs before retiring workspaces')
        projects=[dict(row) for row in connection.execute('SELECT id,user_id,openhands_project_id FROM coding_projects')]
        receipt={'version':1,'at':time.time(),'projects':projects,'jobs':[row['id'] for row in rows],
                 'backup':str(backup),'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest()}
        for row in rows:
            job=json.loads(row['job_json'] or '{}')
            job.update(worker_workspace_available=False,worker_workspace_reason='Codebox was retired; its unused workspace was deliberately discarded. Retained artifacts and accepted revisions remain available.')
            connection.execute('UPDATE coder_workflows SET job_json=? WHERE id=?',(json.dumps(job),row['id']))
        temporary=registry.with_suffix('.pending');temporary.write_text(json.dumps(receipt,indent=2)+'\n');temporary.chmod(0o600)
        if os.geteuid() == 0:
            owner=database.stat();os.chown(temporary,owner.st_uid,owner.st_gid)
        connection.commit()
        temporary.replace(registry)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--registry',type=Path,required=True)
    args=parser.parse_args()
    receipt=mark(args.database,args.registry)
    print(json.dumps({'jobs':len(receipt['jobs']),'projects':len(receipt['projects']),'backup':receipt['backup']}))


if __name__=='__main__':main()
