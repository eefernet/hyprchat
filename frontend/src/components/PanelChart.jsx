import React, { useEffect, useRef, useState } from 'react';

import { Skeleton } from './Skeleton.jsx';

// Reusable Chart.js host for panel dashboards (AnalyticsPanel etc.).
// Caller supplies a full Chart.js `data` object (labels + styled datasets);
// themed defaults (legend/ticks/grid/tooltip) mirror ChartBlock in
// markdownBlocks.jsx. Chart.js stays in its lazy vendor chunk via ensureChart().
export default function PanelChart({t,font,type="bar",data,options={},height=260,title}){
  const canvasRef=useRef(null);
  const chartRef=useRef(null);
  const [tick,setTick]=useState(0); // bumped when the lazy chart.js chunk loads
  useEffect(()=>{
    if(!window.Chart){window.ensureChart&&window.ensureChart().then(()=>setTick(x=>x+1));return;}
    if(!canvasRef.current)return;
    const isCircular=["pie","doughnut","polarArea"].includes(type);
    if(chartRef.current){try{chartRef.current.destroy();}catch{}chartRef.current=null;}
    try{
      chartRef.current=new window.Chart(canvasRef.current,{
        type,
        data,
        options:{
          responsive:true,
          maintainAspectRatio:false,
          plugins:{
            legend:{labels:{color:t.dim,font:{family:font,size:11},boxWidth:12,boxHeight:12}},
            title:title?{display:true,text:title,color:t.text,font:{family:font,size:12,weight:"bold"},padding:{bottom:10}}:{display:false},
            tooltip:{bodyFont:{family:font},titleFont:{family:font},backgroundColor:`${t.surface}EE`,borderColor:t.brd,borderWidth:1,titleColor:t.text,bodyColor:t.dim},
            ...(options.plugins||{}),
          },
          scales:isCircular?{}:{
            x:{ticks:{color:t.mut,font:{family:font,size:10}},grid:{color:`${t.brd}33`},...(options.scales?.x||{})},
            y:{ticks:{color:t.mut,font:{family:font,size:10}},grid:{color:`${t.brd}33`},...(options.scales?.y||{})},
          },
          ...Object.fromEntries(Object.entries(options).filter(([k])=>k!=="plugins"&&k!=="scales")),
        },
      });
    }catch{/* chart config error — leave the canvas blank rather than crash the panel */}
    return()=>{if(chartRef.current){try{chartRef.current.destroy();}catch{}chartRef.current=null;}};
  },[type,data,options,t,font,title,tick]);
  return <div style={{height,position:"relative"}}>
    {!window.Chart&&<Skeleton t={t} h="100%" r={10}/>}
    <canvas ref={canvasRef} style={{display:window.Chart?"block":"none"}}/>
  </div>;
}
