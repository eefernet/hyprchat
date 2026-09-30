"""Validate the healthy uploaded fixture before making its documented faults."""
import argparse,hashlib,json,os,subprocess,sys,tarfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evals.policy7_fixtures import node_fixture

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
 root=Path(args.root);root.mkdir(parents=True,exist_ok=False)
 source=root/'fixture';source.mkdir()
 for name,text in node_fixture().items():
  path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
 archive=root/'healthy.tar.gz'
 with tarfile.open(archive,'w:gz') as bundle:
  for path in source.rglob('*'):
   if path.is_file():bundle.add(path,arcname=path.relative_to(source))
 from evals.isolated_judge import run as isolated_judge
 result=isolated_judge(Path(__file__).with_name('policy7_golden.py'),['--folder',str(root),
  '--archive',str(archive),'--scenario','upload-medium'],root,archive,timeout=600)
 (root/'golden-output.log').write_text(result.stdout+result.stderr)
 report=json.loads((root/'golden.json').read_text())
 print(json.dumps(report,indent=2))
 if not report['passed']:raise SystemExit(1)
 (root/'fixture-identity.json').write_text(json.dumps({'source_hashes':{name:hashlib.sha256(text.encode()).hexdigest() for name,text in node_fixture().items()},
  'documented_faults':['status query ignored','summary done count always zero']},indent=2))
if __name__=='__main__':main()
