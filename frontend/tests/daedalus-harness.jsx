import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import DaedalusSettings from '../src/components/DaedalusSettings.jsx';
import DaedalusJobCard from '../src/components/DaedalusJobCard.jsx';
import SidePane from '../src/components/SidePane.jsx';
import useIsMobile from '../src/useIsMobile.js';
import DaedalusRequestOptions from '../src/components/DaedalusRequestOptions.jsx';
const t={text:'#e7edf5',dim:'#b8c5d8',mut:'#91a2b8',acc:'#7cc3ff',err:'#ff9c9c',ok:'#81dbab',brd:'#35475e',surface:'#152031',bgDeep:'#0c1420'};
document.body.style.cssText='background:#0c1420;margin:24px auto;padding:0 20px;max-width:900px;font-family:system-ui';
function Harness(){const [options,setOptions]=useState({});const isMobile=useIsMobile();return <div style={{display:"flex",position:"relative",height:"calc(100vh - 48px)"}}><main style={{flex:1,minWidth:0,overflow:"auto"}}><DaedalusRequestOptions conversationId="fixture-conversation" currentProjectId="cp-fixture" onChange={setOptions} t={t} font="system-ui"/><pre aria-label="Request options">{JSON.stringify(options)}</pre><DaedalusJobCard workflow={{id:'fixture-job',workflow_version:3,state:'coding'}} t={t} font="system-ui"/><DaedalusSettings t={t} font="system-ui"/></main><SidePane t={t} font="system-ui" isMobile={isMobile}/></div>;}
createRoot(document.getElementById('root')).render(<Harness/>);
