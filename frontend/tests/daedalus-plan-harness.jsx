// Renders the job card from real (trimmed) job snapshots with the API stubbed, for visual QA.
import React from 'react';
import {createRoot} from 'react-dom/client';
import DaedalusJobCard from '../src/components/DaedalusJobCard.jsx';
import fixtures from '../src/daedalusProgress.fixtures.json';
const t={text:'#e7edf5',dim:'#b8c5d8',mut:'#91a2b8',acc:'#7cc3ff',err:'#ff9c9c',ok:'#81dbab',brd:'#35475e',surface:'#152031',bgDeep:'#0c1420'};
const running={id:'running',workflow_version:3,state:'coding',policy_version:7,builder:'sdk',build_continuations:1,repair_round:0,model:'qwen3-coder:30b',
  calls_used:24,seconds_used:192,project_name:'Local Task Board',inventory:{files:4},user_task:'**Tech Stack:**\n- Node.js + Express\n- SQLite',
  brief:{outcomes:[{id:'o1',text:'REST API with CRUD endpoints for tasks',evidence_types:['behavior','tests']},{id:'o2',text:'Three-column Kanban board with drag and drop',evidence_types:['behavior']},
    {id:'o3',text:'Tasks persist in a SQLite database',evidence_types:['behavior','tests']},{id:'o4',text:'README with install and startup instructions',evidence_types:['documentation']}],
    batches:[{task:'Initialize project structure and backend',files:['package.json','server.js','database.js']},{task:'Create frontend HTML, CSS and JavaScript',files:['public/index.html','public/style.css','public/app.js']},{task:'Add tests and README',files:['tests/api.test.js','README.md']}]},
  last_patch:{changed:['server.js'],source_hashes:{'package.json':'h','server.js':'h','database.js':'h','public/index.html':'h'}},checks:[]};
const stopped={...running,id:'stopped',state:'cancelled',calls_used:22,seconds_used:130};
const jobs={running,stopped,...Object.fromEntries(Object.entries(fixtures).map(([key,job])=>[key,{...job,id:key,workflow_version:3}]))};
const realFetch=window.fetch.bind(window);
window.fetch=async(url,options)=>{
  const match=String(url).match(/\/api\/coder\/workflows\/([\w-]+)(\/events)?/);
  if(match&&match[2])return new Response('',{status:200,headers:{'Content-Type':'text/event-stream'}});
  if(match&&jobs[match[1]])return new Response(JSON.stringify(jobs[match[1]]),{status:200,headers:{'Content-Type':'application/json'}});
  return realFetch(url,options);
};
document.body.style.cssText='background:#0c1420;margin:24px auto;padding:0 16px;max-width:900px;font-family:system-ui';
const only=new URLSearchParams(location.search).get('job');
createRoot(document.getElementById('root')).render(<>{Object.keys(jobs).filter(key=>!only||key===only).map(key=>
  <div key={key} data-job={key}><h3 style={{color:'#91a2b8',font:'12px system-ui',margin:'18px 0 4px'}}>{key}</h3>
    <DaedalusJobCard workflow={jobs[key]} t={t} font="system-ui" md={text=><span style={{whiteSpace:'pre-wrap'}}>{text}</span>}/></div>)}</>);
