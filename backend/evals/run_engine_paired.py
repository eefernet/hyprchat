"""Interleave the frozen everyday user prompts on two prepared, isolated releases."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from engine_cases_everyday import CASES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    configs=[json.loads(p.read_text()) for p in (args.previous,args.candidate)]
    common=('model','model_digest','chat_model','chat_digest','profiles','settings','judge_sha256',
            'judge_bank_sha256','sdk_version','limits','cache_policy','judge_identity',
            'prompt_bank_version','judge_adapter_sha256')
    if any(not configs[0].get(k) or configs[0][k]!=configs[1].get(k) for k in common):
        raise ValueError('The two arms have different or missing frozen model, resource or judge identities')
    if configs[0]['url']==configs[1]['url'] or configs[0]['release_id']==configs[1]['release_id']:
        raise ValueError('Use two distinct candidate releases and test origins')
    arms=[('previous',args.previous),('candidate',args.candidate)]
    runner=Path(__file__).with_name('run_engine_chat.py')
    for rep in range(1,4):
        for index,case in enumerate(CASES):
            slot=f"{case['id']}-{rep}"
            for arm,config in (arms if (index+rep)%2 else reversed(arms)):
                subprocess.run([sys.executable,str(runner),'--cohort','everyday','--slot',slot,
                                '--environment',str(config),'--output',str(args.output/arm)],check=True)
    grader=Path(__file__).resolve().parents[2]/'scripts/qualify_engine_report.py'
    subprocess.run([sys.executable,str(grader),str(args.output/'candidate/report.json'),'--require-composer',
                    '--compare',str(args.output/'previous/report.json'),'--output',str(args.output/'comparison.json')],check=True)


if __name__=='__main__':
    main()
