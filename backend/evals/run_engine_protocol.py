"""Serialized Qwen protocol/compaction matrix through the worker inference bridge."""
import argparse
import ast
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coder_worker_runtime import WorkerStore
from coder_sandbox_agent import model_bridge
from coder_engine_ops import prepare
from coder_engine_tokens import MODEL_DIGEST
from context_policy import DEFAULTS

ADD=[{'type':'function','function':{'name':'add','description':'Add two integers.',
      'parameters':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b']}}}]
WRITE=[{'type':'function','function':{'name':'write_file','description':'Write the complete file content.',
        'parameters':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'}},'required':['path','content']}}}]


def save(path,data):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(data,indent=2,default=str)+'\n');temporary.replace(path)


def main():
    if not __debug__:
        raise RuntimeError('Protocol qualification requires Python assertions enabled')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--ollama',default='http://192.168.1.110:11434')
    parser.add_argument('--model',default='qwen3.6-35b-a3b-gguf:latest')
    parser.add_argument('--limits',type=int,nargs='+',default=[4096,8192,16384])
    parser.add_argument('--settings',type=Path,help='Frozen candidate Settings for a single release configuration')
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    path=root/'report.json'
    if path.exists():raise ValueError('Use a new matrix directory; never overwrite a prior result')
    lock=(root.parent/'inference-evaluation.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store=WorkerStore(root/'worker')
    report={'model':args.model,'digest':MODEL_DIGEST,'context':65536,'temperature':.2,'thinking':True,'seed':None,'rows':[]}
    if args.settings:
        from context_policy import resolve
        saved = {**DEFAULTS, **json.loads(args.settings.read_text())}
        policy = resolve('builder', saved)
        if args.limits != [policy.num_predict] or resolve('compaction', saved).num_predict != policy.num_predict:
            raise ValueError('Release protocol limits must match saved coding and compaction settings')
        report.update(context=policy.num_ctx, settings=saved)
    for output in args.limits:
        settings={**DEFAULTS,'context_compaction':'on','openhands_num_ctx':65536,
                  'daedalus_role_contexts':{'builder':65536,'compaction':65536},
                  'daedalus_role_outputs':{'builder':output,'compaction':output},
                  'daedalus_role_thinking':{'builder':'on','compaction':'on'}}
        if args.settings:
            settings=saved
        for repeat in range(3):
            for name in ('native_tool','tool_continuation','large_edit','reasoning','compaction','termination'):
                identity=f'{output}-{repeat+1}-{name}'
                payload={'engine':'openhands159','engine_behavior_version':1,'policy_version':8,'settings':settings,'model':args.model,'model_digest':MODEL_DIGEST,
                         'ollama_url':args.ollama,'seconds_remaining':600,'calls_remaining':12}
                store.create(identity,'protocol','code',payload);store.update(identity,status='running',started=time.time())
                row={'case':name,'output_limit':output,'repeat':repeat+1,'started':time.time(),'passed':False}
                try:
                    prepare(store,identity)
                    from context_policy import resolve, operation_settings
                    assert resolve('builder', operation_settings(payload)).num_predict == output
                    with model_bridge(store,identity) as bridge:
                        def chat(messages,tools=(),role='builder',requirement='text_allowed'):
                            current = store.get(identity)['payload']
                            store.update(identity, payload={**current, 'response_requirements':{role:requirement}})
                            body={'model':args.model,'messages':messages,'tools':list(tools),'think':True,'stream':False,'options':{'temperature':.2}}
                            req=urllib.request.Request(bridge+'/api/chat',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Daedalus-Role':role})
                            try:
                                with urllib.request.urlopen(req,timeout=590) as response: result=json.load(response)
                            except urllib.error.HTTPError as error:raise RuntimeError(error.read().decode()) from error
                            row.setdefault('responses',[]).append(result)
                            assert result.get('done') and result.get('done_reason')=='stop', 'Generation did not terminate naturally'
                            return result['message']
                        if name in {'native_tool','tool_continuation'}:
                            messages=[{'role':'user','content':'Call add with a=17 and b=25. After the tool returns, reply with only its result.'}]
                            message=chat(messages,ADD,requirement='action_required');calls=message.get('tool_calls',[])
                            assert len(calls)==1 and calls[0]['function']['name']=='add' and calls[0]['function']['arguments']=={'a':17,'b':25},message
                            if name=='tool_continuation':
                                messages += [message,{'role':'tool','tool_name':'add','content':'42'}]
                                final=chat(messages,ADD)
                                assert final.get('content','').strip()=='42' and not final.get('tool_calls'),final
                        elif name=='large_edit':
                            prompt=('Call write_file once to create helpers.py containing exactly 180 ordinary Python function definitions, '
                                    'named helper_0 through helper_179. Each has one argument value and returns value + its numeric suffix. '
                                    'Write all definitions explicitly, no loops, generators, exec or ellipses. No docstrings or comments are needed.')
                            message=chat([{'role':'user','content':prompt}],WRITE,requirement='action_required');calls=message.get('tool_calls',[])
                            assert len(calls)==1 and calls[0]['function']['name']=='write_file',message
                            args_=calls[0]['function']['arguments'];tree=ast.parse(args_['content'])
                            assert args_['path']=='helpers.py' and len(tree.body)==180
                            for i,node in enumerate(tree.body):
                                assert isinstance(node,ast.FunctionDef) and node.name==f'helper_{i}'
                                assert len(node.body)==1 and isinstance(node.body[0],ast.Return)
                                value=node.body[0].value
                                assert isinstance(value,ast.BinOp) and isinstance(value.op,ast.Add) and isinstance(value.left,ast.Name) and value.left.id=='value' and isinstance(value.right,ast.Constant) and value.right.value==i
                        elif name=='compaction':
                            from openhands.sdk import LLM, Message, TextContent
                            from openhands.sdk.event.llm_convertible import MessageEvent
                            from openhands.sdk.context.view import View
                            from coder_engine_condenser import CountedLLM,EngineCondenser
                            common=dict(model='ollama_chat/'+args.model,base_url=bridge,max_input_tokens=65536,max_output_tokens=output,
                                        temperature=.2,native_tool_calling=True,reasoning_effort=None,num_retries=0,timeout=590,
                                        capability_overrides={'supports_sampling_params':True},litellm_extra_body={'think':True})
                            llm=CountedLLM(extra_headers={'X-Daedalus-Role':'builder'},**common)
                            summary=LLM(extra_headers={'X-Daedalus-Role':'compaction'},**common)
                            condenser=EngineCondenser(llm=summary,max_tokens=2048,max_size=12,keep_first=2)
                            events=[MessageEvent(llm_message=Message(role='user' if i%2==0 else 'assistant',content=[TextContent(text='Remember the project is called copper. '+('Observed implementation and tests; '*250))]),source='user' if i%2==0 else 'agent') for i in range(10)]
                            result=condenser.condense(View(events=events),agent_llm=llm)
                            row['compaction']=result.model_dump(mode='json')
                            assert type(result).__name__=='Condensation',type(result).__name__
                        else:
                            expected='READY' if name=='reasoning' else 'DONE'
                            message=chat([{'role':'user','content':'Reply with only the following word, without punctuation or formatting:\n'+expected}])
                            assert message.get('content','').strip()==expected and not message.get('tool_calls'),message
                            if name=='reasoning':assert message.get('thinking'), 'Reasoning missing'
                    row['passed']=True
                except Exception as error:
                    row['error']=str(error)[:4000]
                finally:
                    store.update(identity,status='succeeded' if row['passed'] else 'failed',ended=time.time())
                row.update(seconds=time.time()-row['started'],calls=store.get(identity)['calls'])
                with store.connect() as connection:
                    events=[dict(x) for x in connection.execute('SELECT * FROM events WHERE operation_id=?',(identity,))]
                row['events']=events
                row['first_attempt_passed']=row['passed'] and not any('model_failure' in str(x) for x in events)
                row['recovered']=row['passed'] and not row['first_attempt_passed']
                report['rows'].append(row);save(path,report)
                print(json.dumps({k:v for k,v in row.items() if k not in {'responses','events','compaction'}}),flush=True)
    report['passing_limits']=[limit for limit in args.limits if all(r['passed'] for r in report['rows'] if r['output_limit']==limit)]
    report['selected_protocol_limit']=min(report['passing_limits']) if report['passing_limits'] else None
    report['completed']=time.time();save(path,report)
    if args.settings and not report['passing_limits']:
        raise SystemExit(1)


if __name__=='__main__':main()
