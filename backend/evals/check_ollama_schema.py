"""Live grammar compatibility checks, separate from model qualification scores."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
import jsonschema
from coder_check_schema import probe_schema
from coder_contracts import inspection_schema, normalize_probe


def run(url, model, output):
    common={'cwd':'.','expected_behavior':'Exercise this runner schema'}
    cases=[
        {'runner':'api','target':{'language':'python','path':'subject.py','export':'subject'},
         'cases':[{'args':[[1,False,None,{'nested':[2.5,'x']}]],
            'expect':{'kind':'equal','value':[1,False,None,{'nested':[2.5,'x']}]},'preserve_inputs':True}]},
        {'runner':'file','path':'README.md','assertions':[{'kind':'nonempty'},{'kind':'contains','value':'Usage'}]},
        {'runner':'browser','path':'/index.html','steps':[{'action':'text','selector':'h1','value':'Ready'}]},
        {'runner':'python','program':'from subject import subject\nassert subject() == 1','bindings':[{'path':'subject.py','export':'subject'}]},
        {'runner':'node','program':"import {subject} from './subject.mjs'; import assert from 'node:assert/strict'; assert.equal(subject(),1);",'bindings':[{'path':'subject.mjs','export':'subject'}]},
        {'runner':'shell','program':'python3 cli.py --help','bindings':[{'path':'cli.py'}]},
    ]
    metadata=requests.get(url+'/api/version',timeout=15);metadata.raise_for_status()
    report={'model':model,'runtime':metadata.json(),'cases':[],'num_ctx':65536,'num_predict':2048,'thinking':False}
    schema=inspection_schema(probe_schema())
    for spec in cases:
        expected={**common,**spec};started=time.time()
        response=requests.post(url+'/api/chat',json={'model':model,'stream':False,'think':False,'format':schema,
            'options':{'num_ctx':65536,'num_predict':2048,'temperature':0},
            'messages':[{'role':'user','content':'Repeat exactly this JSON object without changing any value, wrapper, or field: '+json.dumps(expected)}]},timeout=240)
        record={'runner':spec['runner'],'status_code':response.status_code,'seconds':time.time()-started,'passed':False}
        try:
            response.raise_for_status();body=response.json();record['response']=body
            answer=json.loads(body['message']['content']);jsonschema.validate(answer,schema)
            normalize_probe(answer,{'id':'r1','kind':'documentation' if spec['runner']=='file' else 'behavior'},'c',5)
            record['passed']=answer==expected
            if not record['passed']:record['error']='Runtime changed the requested JSON values'
        except Exception as error: record['error']=str(error)
        report['cases'].append(record)
        report['passed']=len(report['cases'])==len(cases) and all(c['passed'] for c in report['cases'])
        Path(output).write_text(json.dumps(report,indent=2))
        print(json.dumps({k:v for k,v in record.items() if k!='response'}),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('url','model','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.url.rstrip('/'),args.model,args.output) else 1)
