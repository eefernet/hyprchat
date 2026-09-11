import React from 'react';
import {createRoot} from 'react-dom/client';
import DaedalusSettings from '../src/components/DaedalusSettings.jsx';
import DaedalusJobCard from '../src/components/DaedalusJobCard.jsx';
const t={text:'#e7edf5',dim:'#b8c5d8',mut:'#91a2b8',acc:'#7cc3ff',err:'#ff9c9c',ok:'#81dbab',brd:'#35475e',surface:'#152031',bgDeep:'#0c1420'};
document.body.style.cssText='background:#0c1420;margin:24px auto;padding:0 20px;max-width:900px;font-family:system-ui';
createRoot(document.getElementById('root')).render(<><DaedalusJobCard workflow={{id:'fixture-job',workflow_version:3,state:'coding'}} t={t} font="system-ui"/><DaedalusSettings t={t} font="system-ui"/></>);
