import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {PlanSection,ChecksSection,StageRail,ResultBanner} from '../src/components/DaedalusPlanPanel.jsx';
const base={engine:'openhands159',state:'ready_for_review',revision_id:'r',progress:{version:1,plan:[{title:'Create project files',status:'done'},{title:'Run tests and fix failures',status:'in_progress'},{title:'Verify application behavior',status:'todo'}]},verification_summary:{revision_id:'r',checks_passed:true},candidate_artifact:{metadata:{checks_passed:true}},checks:[...Array.from({length:8},(_,i)=>({id:`check:${i}`,revision_id:'r',passed:true})),...Array.from({length:2},(_,i)=>({id:`workflow:${i}`,revision_id:'r',passed:true,workflow_status:'advisory',reason:'This optional workflow probe was inconclusive.'}))]};
document.body.style.cssText='background:#0c1420;color:#e7edf5;margin:24px auto;padding:0 16px;max-width:900px;font-family:system-ui';
function App(){const [tick,setTick]=useState(0);return <main><h1>Daedalus progress QA</h1><button onClick={()=>setTick(tick+1)}>Simulate live update</button><p>Updates: {tick}</p><StageRail job={base}/><ResultBanner job={base}/><PlanSection job={{...base,updated_at:tick}}/><ChecksSection job={base}/></main>;}
createRoot(document.getElementById('root')).render(<App/>);
