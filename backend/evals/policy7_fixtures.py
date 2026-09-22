"""Matched public requests and tiny uploaded repositories; goldens live separately."""
WEB_TASKS=[
 'Build a dependency-free expense tracker in index.html. Inputs #amount (decimal money), #category, #date (YYYY-MM-DD), and button #add-expense add rows to #expenses. Display the sum with two decimal places in #total. Preserve expenses across reloads with localStorage. Render entered text literally. Add a README and executable browser regression checks.',
 'Add an Edit button to each expense row. Clicking it loads that row into the existing inputs; #save-expense saves its changed amount, category and date without adding a duplicate. Preserve adding expenses and reload persistence, and recalculate #total. Add browser regression checks.',
 'Add a button #export-csv that downloads expenses.csv with header date,category,amount, one row per expense, two-decimal amounts and correct CSV quoting. Preserve adding, editing, totals and reload persistence. Document export and add regression checks.',
]
CLI_TASKS=[
 'Fix ledger.py so python3 ledger.py expenses.csv sums every CSV data row using Decimal and prints a two-decimal total. CSV columns are category,amount. Preserve VERSION and the existing CLI. The executable test command is python3 tests/check_ledger.py; keep it runnable as a script. Add a README and tests.',
 'Add optional --category CATEGORY filtering and --json output to ledger.py. JSON must contain total as a two-decimal string and count as an integer number of matching rows. No matching rows returns total "0.00" and count 0. Preserve the existing plain total output and VERSION. Extend the executable regression tests and documentation.',
]
CLI_FILES={
 'ledger.py':'''import argparse,csv
from decimal import Decimal
VERSION = "keep-me"
def total(path):
    with open(path,newline="") as source:
        rows=csv.DictReader(source)
        next(rows,None)
        return sum((Decimal(row["amount"]) for row in rows),Decimal(0))
if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("path")
    args=parser.parse_args();print(f"{total(args.path):.2f}")
''',
 'tests/check_ledger.py':'''import pathlib,subprocess,sys,tempfile
root=pathlib.Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as work:
    csv=pathlib.Path(work)/'expenses.csv'
    csv.write_text('category,amount\\nfood,0.10\\ntravel,0.20\\n')
    result=subprocess.run([sys.executable,str(root/'ledger.py'),str(csv)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='0.30',result.stdout
''',
 'README.md':'Run python3 ledger.py expenses.csv. Run tests with python3 tests/check_ledger.py.\n',
}
TODO_FILES={
 'index.html':'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Todos</title><link rel="stylesheet" href="styles.css"><label>Task<input id="task"></label><button id="add">Add</button><ul id="todos"></ul><script type="module" src="app.js"></script>',
 'package.json':'{"name":"uploaded-todos","type":"module","scripts":{"test":"node --test tests/*.test.js"}}',
 'model.js':'export function dropTodo(items,id) { return items; }\n',
 'app.js':'''import {dropTodo} from './model.js';
let items=[];
function render(){const ul=document.querySelector('#todos');ul.replaceChildren();for(const item of items){const li=document.createElement('li');const span=document.createElement('span');span.textContent=item.text;li.append(span);const b=document.createElement('button');b.textContent='Delete';b.onclick=()=>{items=dropTodo(items,item.id);render()};li.append(b);ul.append(li)}}
document.querySelector('#add').onclick=()=>{items.push({id:String(Date.now()),text:document.querySelector('#task').value});render()};render();
''',
 'styles.css':'body { font-family: sans-serif; max-width: 48rem; margin: 2rem auto; padding: 1rem; }\n',
 'tests/model.test.js':'''import test from 'node:test';import assert from 'node:assert/strict';import {dropTodo} from '../model.js';
test('remove matching ID without mutating input',()=>{const items=[{id:'1',text:'A'},{id:'2',text:'B'}];assert.deepEqual(dropTodo(items,'1'),[{id:'2',text:'B'}]);assert.equal(items.length,2)});
''',
}
TODO_TASK='Fix this static todo app to persist todos in localStorage across reloads and make Delete remove the matching item permanently. Keep #task, #add and #todos. Render entered text literally. Preserve the dropTodo(items,id) API and leave styles.css byte-for-byte unchanged. Keep npm test passing, add executable browser regression checks and a README with run instructions. package.json provides test tooling only; index.html must remain previewable as a static app.'
MEDIUM_TASK='''Build a medium-size project and task board using a React/Vite frontend, FastAPI backend and SQLite storage. No authentication or external services are needed. Use app.py exporting the FastAPI app, with APP_DB_PATH selecting the SQLite file. Serve the built frontend at / from the same backend. Document installing dependencies, building the frontend and running python3 -m uvicorn app:app --host 127.0.0.1 --port 8000.
Provide these JSON APIs: POST /api/projects {name} and GET /api/projects; POST /api/tasks {project_id,title,priority,status}, GET /api/tasks with optional project_id,status,q filters, PATCH /api/tasks/{id} for title,priority,status, and DELETE /api/tasks/{id}. Responses for creation include id; list responses are JSON arrays. Task status is todo,doing,done and priority is low,medium,high. Reject empty names/titles and invalid values with 400 or 422; use 404 for unknown IDs. GET /api/summary returns {total,todo,doing,done} counts over all tasks. Data must survive a backend restart.
The responsive UI lets users create/select projects, add/edit/delete tasks, search titles, filter status and see summary counts. Use accessible labels Project name, Task title, Priority, Status, Search, Status filter, and buttons Create project and Add task. Show Edit and Delete buttons for each task, and Save task when editing. Render user text literally. Include a README, executable API tests and browser regression checks. Keep the project organized into ordinary backend, frontend components and test files.'''
SCENARIOS=[('web',WEB_TASKS,{}),('medium',[MEDIUM_TASK],{}),('upload-python',CLI_TASKS,CLI_FILES),('upload-web',[TODO_TASK],TODO_FILES)]

# Healthy uploaded medium fixture. Validate this before injecting the two faults.
NODE_FILES={
'package.json':'{"name":"local-task-board","version":"1.0.0","scripts":{"start":"node server.js","test":"python3 tests/test_api.py"},"dependencies":{"express":"4.21.2","better-sqlite3":"11.10.0"}}',
'.daedalus-run.json':'{"services":[{"id":"app","cwd":".","command":"PORT={port} node server.js","ready_path":"/"}]}',
'VERSION':'medium-fixture-v1\n',
'server.js':'''const express=require('express');
const Database=require('better-sqlite3');
const path=require('node:path');
const app=express();app.use(express.json());
const db=new Database(process.env.APP_DB_PATH||'board.sqlite');
db.pragma('foreign_keys = ON');
db.exec(`CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),title TEXT NOT NULL,priority TEXT NOT NULL,status TEXT NOT NULL);`);
const statuses=['todo','doing','done'],priorities=['low','medium','high'];
app.get('/api/projects',(req,res)=>res.json(db.prepare('SELECT * FROM projects ORDER BY id').all()));
app.post('/api/projects',(req,res)=>{const name=String(req.body.name||'').trim();if(!name)return res.status(400).json({error:'name required'});const id=db.prepare('INSERT INTO projects(name) VALUES(?)').run(name).lastInsertRowid;res.status(201).json({id,name});});
app.get('/api/tasks',(req,res)=>{let rows=db.prepare('SELECT * FROM tasks ORDER BY id').all();if(req.query.project_id)rows=rows.filter(t=>String(t.project_id)===req.query.project_id);if(req.query.status)rows=rows.filter(t=>t.status===req.query.status);if(req.query.q)rows=rows.filter(t=>t.title.toLowerCase().includes(req.query.q.toLowerCase()));res.json(rows);});
app.post('/api/tasks',(req,res)=>{const {project_id,priority,status}=req.body,title=String(req.body.title||'').trim();if(!title||!statuses.includes(status)||!priorities.includes(priority)||!db.prepare('SELECT id FROM projects WHERE id=?').get(project_id||-1))return res.status(400).json({error:'invalid task'});const id=db.prepare('INSERT INTO tasks(project_id,title,priority,status) VALUES(?,?,?,?)').run(project_id,title,priority,status).lastInsertRowid;res.status(201).json(db.prepare('SELECT * FROM tasks WHERE id=?').get(id));});
app.patch('/api/tasks/:id',(req,res)=>{const task=db.prepare('SELECT * FROM tasks WHERE id=?').get(req.params.id);if(!task)return res.status(404).json({error:'missing task'});const next={...task,...req.body};if(!String(next.title||'').trim()||!statuses.includes(next.status)||!priorities.includes(next.priority))return res.status(400).json({error:'invalid task'});db.prepare('UPDATE tasks SET title=?,priority=?,status=? WHERE id=?').run(next.title.trim(),next.priority,next.status,task.id);res.json(db.prepare('SELECT * FROM tasks WHERE id=?').get(task.id));});
app.delete('/api/tasks/:id',(req,res)=>{const result=db.prepare('DELETE FROM tasks WHERE id=?').run(req.params.id);res.status(result.changes?204:404).end();});
app.get('/api/summary',(req,res)=>{const rows=db.prepare('SELECT status,COUNT(*) AS n FROM tasks GROUP BY status').all();const result={total:0,todo:0,doing:0,done:0};for(const row of rows){result[row.status]=row.n;result.total+=row.n;}res.json(result);});
app.use(express.static(path.join(__dirname,'public')));
app.listen(Number(process.env.PORT||8000),'127.0.0.1');
''',
'public/index.html':'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Local Task Board</title><link rel="stylesheet" href="styles.css"></head><body><h1>Local Task Board</h1><form id="projects"><label>Project name<input id="project-name" required></label><button>Create project</button></form><form id="tasks"><label>Project<select id="project" aria-label="Project"></select></label><label>Task title<input id="title" required></label><label>Priority<select id="priority" aria-label="Priority"><option>low</option><option>medium</option><option>high</option></select></label><label>Status<select id="status" aria-label="Status"><option>todo</option><option>doing</option><option>done</option></select></label><button id="save">Add task</button></form><label>Search<input id="search"></label><label>Status filter<select id="filter" aria-label="Status filter"><option value="">All</option><option>todo</option><option>doing</option><option>done</option></select></label><p id="summary"></p><ul id="rows"></ul><p id="error" role="alert"></p><script src="app.js"></script></body></html>''',
'public/styles.css':'body{font-family:system-ui;max-width:60rem;margin:2rem auto;padding:1rem}form{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}label{display:flex;flex-direction:column;gap:.3rem}input,select,button{font:inherit;padding:.5rem;max-width:100%;box-sizing:border-box}li{margin:1rem 0;overflow-wrap:anywhere}li button{margin-left:.5rem}\n',
'public/app.js':'''const el=id=>document.getElementById(id);let editing=null;
async function api(url,method='GET',data){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});if(!r.ok)throw new Error('Request failed '+r.status);return r.status===204?null:r.json();}
async function refreshProjects(){const selected=el('project').value;const projects=await api('/api/projects');el('project').replaceChildren(...projects.map(p=>{const o=document.createElement('option');o.value=p.id;o.textContent=p.name;return o}));if(selected)el('project').value=selected;}
async function refresh(){const query=new URLSearchParams({q:el('search').value,status:el('filter').value});const tasks=await api('/api/tasks?'+query);el('rows').replaceChildren();for(const task of tasks){const li=document.createElement('li'),text=document.createElement('span');text.textContent=task.title+' · '+task.status+' · '+task.priority;li.append(text);for(const label of ['Edit','Delete']){const b=document.createElement('button');b.textContent=label;b.onclick=async()=>{if(label==='Delete'){await api('/api/tasks/'+task.id,'DELETE');await refresh();}else{editing=task.id;el('title').value=task.title;el('priority').value=task.priority;el('status').value=task.status;el('project').value=task.project_id;el('save').textContent='Save task';}};li.append(b);}el('rows').append(li);}const s=await api('/api/summary');el('summary').textContent=`Total ${s.total} · To do ${s.todo} · Doing ${s.doing} · Done ${s.done}`;}
el('projects').onsubmit=async e=>{e.preventDefault();const p=await api('/api/projects','POST',{name:el('project-name').value});await refreshProjects();el('project').value=p.id;el('project-name').value='';};
el('tasks').onsubmit=async e=>{e.preventDefault();await api('/api/tasks'+(editing?'/'+editing:''),editing?'PATCH':'POST',{project_id:Number(el('project').value),title:el('title').value,priority:el('priority').value,status:el('status').value});editing=null;el('save').textContent='Add task';el('title').value='';await refresh();};
el('search').oninput=refresh;el('filter').onchange=refresh;refreshProjects().then(refresh).catch(e=>el('error').textContent=e.message);
''',
'README.md':'''# Local Task Board
Run npm install, then PORT=8000 node server.js. APP_DB_PATH selects the SQLite database. Open http://127.0.0.1:8000. The existing JSON APIs and vanilla frontend are public interfaces. Run DAEDALUS_APP_URL=http://127.0.0.1:8000 python3 tests/test_api.py against a disposable database. Browser checks use Playwright with the same URL. Data survives restarts. VERSION and public/styles.css must remain unchanged.
''',
'tests/test_api.py':'''import os,json,urllib.request
url=os.environ['DAEDALUS_APP_URL']
def call(path,method='GET',data=None):
 request=urllib.request.Request(url+path,data=json.dumps(data).encode() if data else None,method=method,headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(request) as r:return json.load(r) if r.status!=204 else None
p=call('/api/projects','POST',{'name':'Regression'})
a=call('/api/tasks','POST',{'project_id':p['id'],'title':'Regression alpha','priority':'high','status':'todo'})
b=call('/api/tasks','POST',{'project_id':p['id'],'title':'Regression beta','priority':'low','status':'done'})
try:
 assert [t['id'] for t in call('/api/tasks?project_id='+str(p['id'])+'&q=alpha')]==[a['id']]
 assert [t['id'] for t in call('/api/tasks?project_id='+str(p['id'])+'&status=done')]==[b['id']]
 assert call('/api/summary')['done']>=1
finally:
 call('/api/tasks/'+str(a['id']),'DELETE');call('/api/tasks/'+str(b['id']),'DELETE')
''',
}
NODE_REPAIR_TASK='''Repair the uploaded Node/Express/SQLite task board. Two documented regressions: GET /api/tasks ignores its status filter, and GET /api/summary always reports zero done tasks. Fix both, keep the existing JSON APIs, existing UI and SQLite schema/data compatible, and keep all unrelated behavior. Preserve VERSION and public/styles.css byte-for-byte. Keep the executable API tests runnable; add regression coverage for both faults and update the README. Use the existing Node server, not a replacement Python application.'''
NODE_FEATURE_TASK='''Add optional priority=low|medium|high filtering to GET /api/tasks and an accessible Priority filter select in the existing frontend. Combine it with existing status, project_id and q filters. Preserve existing data, APIs, working UI, VERSION and public/styles.css. Add executable API regression tests and document the filter.'''

def node_fixture(broken=False):
    files=dict(NODE_FILES)
    if broken:
        files['server.js']=files['server.js'].replace("if(req.query.status)rows=rows.filter(t=>t.status===req.query.status);",'/* regression: status filter omitted */')
        files['server.js']=files['server.js'].replace('res.json(result);','result.done=0;res.json(result);')
    return files
