import React from 'react';

import { IC } from '../components/icons.jsx';
import { EmptyState } from '../components/hyprChatWidgets.jsx';
import { SkeletonCard } from '../components/Skeleton.jsx';
import PanelHeader from '../components/PanelHeader.jsx';
import PanelChart from '../components/PanelChart.jsx';

const CHART_FONT="'JetBrains Mono',monospace";

export default function AnalyticsPanel({
  t,
  btnS,
  cardS,
  analyticsDays,
  setAnalyticsDays,
  analyticsGroup,
  setAnalyticsGroup,
  loadAnalytics,
  analyticsData,
}){
  return <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
        <PanelHeader t={t} icon={<IC.BarChart/>} title="Statistics"
          subtitle="Overall HyprChat usage from recorded chat token telemetry."/>
        <div style={{overflowY:"auto",padding:"20px 28px",flex:1}}>
          <div style={{maxWidth:980}}>
            <div style={{display:"flex",gap:8,marginBottom:16,flexWrap:"wrap",alignItems:"center"}}>
              {[7,30,90,0].map(d=><button key={d} onClick={()=>setAnalyticsDays(d)} style={{...btnS(analyticsDays===d?t.acc:t.mut),padding:"5px 12px"}}>{d===0?"All":`${d}d`}</button>)}
              <div style={{flex:1}}/>
              {["day","model","persona"].map(g=><button key={g} onClick={()=>setAnalyticsGroup(g)} style={{...btnS(analyticsGroup===g?t.acc:t.mut),padding:"5px 12px",textTransform:"capitalize"}}>{g==="persona"?"profile":g}</button>)}
              <button onClick={loadAnalytics} style={{...btnS(t.acc),padding:"5px 10px"}}><IC.Refresh/></button>
            </div>
            {analyticsData?(()=>{
              const summary=analyticsData.summary||{};
              const sumRows=rows=>(rows||[]).reduce((a,d)=>({
                prompt:a.prompt+(d.prompt_tokens||0),
                completion:a.completion+(d.completion_tokens||0),
                total:a.total+(d.total_tokens||0),
                requests:a.requests+(d.request_count||0)
              }),{prompt:0,completion:0,total:0,requests:0});
              const all=sumRows(summary.all_time||[]);
              const today=sumRows(summary.today||[]);
              const month=sumRows(summary.daily_30d||[]);
              const allModels=summary.by_model_all||[];
              const allProfiles=summary.by_persona_all||[];
              const topModel=allModels[0];
              const statCard=(label,value,detail,color=t.acc)=><div style={{...cardS,flex:"1 1 170px",minWidth:160,marginBottom:0}}>
                <div style={{fontSize:10,color:t.mut,textTransform:"uppercase",letterSpacing:1,marginBottom:6,fontWeight:800}}>{label}</div>
                <div style={{fontSize:24,fontWeight:900,color,lineHeight:1.05,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{value}</div>
                <div style={{fontSize:10,color:t.mut,marginTop:5,whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{detail}</div>
              </div>;
              const rows=analyticsData.grouped||[];
              const cost=summary.cost||{};
              const fmtUsd=v=>{const n=Number(v)||0;return n>=1?`$${n.toFixed(2)}`:`$${n.toFixed(3)}`;};
              const hasCost=(cost.all_time||0)>0;
              return <>
              <div style={{display:"flex",gap:12,marginBottom:16,flexWrap:"wrap"}}>
                {statCard("Total Tokens",all.total.toLocaleString(),`${all.requests.toLocaleString()} recorded requests`,t.acc)}
                {statCard("Processed",all.prompt.toLocaleString(),"prompt/context tokens",t.warm)}
                {statCard("Generated",all.completion.toLocaleString(),"assistant output tokens",t.ok)}
                {statCard("Models Used",allModels.length.toLocaleString(),topModel?topModel.model:"no model telemetry",t.pink||t.acc)}
              </div>
              <div style={{display:"flex",gap:12,marginBottom:20,flexWrap:"wrap"}}>
                {statCard("Today",today.total.toLocaleString(),`${today.requests.toLocaleString()} requests`,t.acc2||t.acc)}
                {statCard("Last 30 Days",month.total.toLocaleString(),`${month.requests.toLocaleString()} requests`,t.f1||t.acc)}
                {statCard("Top Model",topModel?.model||"-",`${(topModel?.total_tokens||0).toLocaleString()} tokens`,t.pink||t.acc)}
                {hasCost&&statCard("Cloud Spend",fmtUsd(cost.all_time),`today ${fmtUsd(cost.today)} · 30d ${fmtUsd(cost.last_30d)} (estimate)`,t.warm)}
                {((summary.ratings?.up||0)+(summary.ratings?.down||0))>0&&statCard("Feedback",`👍 ${summary.ratings.up} · 👎 ${summary.ratings.down}`,"thumbs on assistant replies",t.ok)}
              </div>
              {rows.length>0?(()=>{
                const isDay=analyticsGroup==="day";
                const labels=rows.map(d=>d.date?d.date.slice(5):d.model||d.persona_name||"(none)");
                const spanLabel=analyticsDays===0?"(all time)":`(${analyticsDays}d)`;
                const costRows=isDay?rows.filter(d=>(d.cost_usd||0)>0):[];
                const topModels=allModels.slice(0,5);
                const otherTokens=allModels.slice(5).reduce((a,d)=>a+(d.total_tokens||0),0);
                // Fixed slot order — keeps t.acc and t.f1 out of adjacency (measured CVD floor pair); "Other" is always t.mut.
                const sliceColors=[t.acc,t.ok,t.warm,t.pink,t.f4];
                return <>
                <div style={{...cardS,padding:20}}>
                  <div style={{fontSize:12,fontWeight:800,marginBottom:12,color:t.mut}}>Tokens by {analyticsGroup==="persona"?"profile":analyticsGroup} {spanLabel}</div>
                  <PanelChart t={t} font={CHART_FONT} type="bar" height={260}
                    data={{labels,datasets:isDay?[
                      {label:"Prompt",data:rows.map(d=>d.prompt_tokens||0),backgroundColor:`${t.warm}CC`,borderColor:t.bgDeep,borderWidth:1,borderRadius:4},
                      {label:"Completion",data:rows.map(d=>d.completion_tokens||0),backgroundColor:`${t.ok}CC`,borderColor:t.bgDeep,borderWidth:1,borderRadius:4},
                    ]:[
                      {label:"Tokens",data:rows.map(d=>d.total_tokens||0),backgroundColor:`${t.acc}CC`,borderColor:t.bgDeep,borderWidth:1,borderRadius:4},
                    ]}}
                    options={{
                      plugins:{legend:isDay?{labels:{color:t.dim,font:{family:CHART_FONT,size:11},boxWidth:12,boxHeight:12}}:{display:false},tooltip:{mode:"index",intersect:false,bodyFont:{family:CHART_FONT},titleFont:{family:CHART_FONT},backgroundColor:`${t.surface}EE`,borderColor:t.brd,borderWidth:1,titleColor:t.text,bodyColor:t.dim}},
                      scales:{x:{stacked:isDay},y:{stacked:isDay}},
                    }}/>
                </div>
                <div style={{display:"flex",gap:16,marginTop:16,flexWrap:"wrap"}}>
                  {topModels.length>1&&<div style={{...cardS,padding:20,flex:"1 1 300px",minWidth:280,marginBottom:0}}>
                    <div style={{fontSize:12,fontWeight:800,marginBottom:12,color:t.mut}}>Model share (all time)</div>
                    <PanelChart t={t} font={CHART_FONT} type="doughnut" height={230}
                      data={{labels:[...topModels.map(d=>d.model),...(otherTokens>0?["Other"]:[])],
                        datasets:[{data:[...topModels.map(d=>d.total_tokens||0),...(otherTokens>0?[otherTokens]:[])],
                          backgroundColor:[...topModels.map((_,i)=>`${sliceColors[i]}CC`),...(otherTokens>0?[`${t.mut}99`]:[])],
                          borderColor:t.bgDeep,borderWidth:2}]}}
                      options={{cutout:"62%"}}/>
                  </div>}
                  {costRows.length>0&&<div style={{...cardS,padding:20,flex:"2 1 380px",minWidth:300,marginBottom:0}}>
                    <div style={{fontSize:12,fontWeight:800,marginBottom:12,color:t.mut}}>Cloud spend {spanLabel}</div>
                    <PanelChart t={t} font={CHART_FONT} type="line" height={230}
                      data={{labels:costRows.map(d=>(d.date||"").slice(5)),
                        datasets:[{label:"Cost (USD)",data:costRows.map(d=>d.cost_usd||0),borderColor:t.f1,backgroundColor:`${t.f1}14`,borderWidth:2,pointRadius:0,pointHoverRadius:4,tension:.25,fill:true}]}}
                      options={{plugins:{legend:{display:false}},scales:{y:{ticks:{color:t.mut,font:{family:CHART_FONT,size:10},callback:v=>`$${Number(v).toFixed(2)}`}}}}}/>
                  </div>}
                </div>
                </>;
              })():<EmptyState t={t} icon={<IC.BarChart/>} title="No statistics recorded yet" hint="Start chatting to record token telemetry and populate this page."/>}
              {allModels.length>0&&<div style={{...cardS,marginTop:16}}>
                <div style={{fontSize:12,fontWeight:800,marginBottom:8,color:t.mut}}>All-Time Model Breakdown</div>
                <div style={{display:"grid",gridTemplateColumns:`minmax(180px,2fr) repeat(${hasCost?5:4},minmax(80px,1fr))`,gap:8,padding:"6px 0",borderBottom:`1px solid ${t.brd}33`,fontSize:10,fontWeight:800,color:t.mut}}>
                  <div>Model</div><div style={{textAlign:"right"}}>Processed</div><div style={{textAlign:"right"}}>Generated</div><div style={{textAlign:"right"}}>Total</div><div style={{textAlign:"right"}}>Requests</div>{hasCost&&<div style={{textAlign:"right"}}>Cost</div>}
                </div>
                {allModels.slice(0,20).map((d,i)=><div key={i} style={{display:"grid",gridTemplateColumns:`minmax(180px,2fr) repeat(${hasCost?5:4},minmax(80px,1fr))`,gap:8,padding:"6px 0",borderBottom:`1px solid ${t.brd}15`,fontSize:10,alignItems:"center"}}>
                  <div style={{color:t.text,fontWeight:700,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{d.model}</div>
                  <div style={{textAlign:"right",color:t.mut}}>{(d.prompt_tokens||0).toLocaleString()}</div>
                  <div style={{textAlign:"right",color:t.mut}}>{(d.completion_tokens||0).toLocaleString()}</div>
                  <div style={{textAlign:"right",color:t.acc,fontWeight:800}}>{(d.total_tokens||0).toLocaleString()}</div>
                  <div style={{textAlign:"right",color:t.mut}}>{d.request_count||0}</div>
                  {hasCost&&<div style={{textAlign:"right",color:d.cost_usd?t.warm:t.mut,fontWeight:d.cost_usd?800:400}}>{d.cost_usd?fmtUsd(d.cost_usd):"—"}</div>}
                </div>)}
              </div>}
              {allProfiles.length>0&&<div style={{...cardS,marginTop:16}}>
                <div style={{fontSize:12,fontWeight:800,marginBottom:8,color:t.mut}}>Top Profiles</div>
                <div style={{display:"flex",gap:6,flexWrap:"wrap"}}>
                  {allProfiles.slice(0,10).map((d,i)=><span key={i} style={{fontSize:10,padding:"5px 8px",borderRadius:7,background:`${t.surface}66`,border:`1px solid ${t.brd}30`,color:t.dim}}>
                    {d.persona_name||"Default"} · {(d.total_tokens||0).toLocaleString()}
                  </span>)}
                </div>
              </div>}
            </>;
            })():<>
              <div style={{display:"flex",gap:12,marginBottom:16,flexWrap:"wrap"}}>
                {Array.from({length:4},(_,i)=><SkeletonCard key={i} t={t} h={90} lines={1} style={{flex:"1 1 170px",minWidth:160}}/>)}
              </div>
              <div style={{display:"flex",gap:12,marginBottom:20,flexWrap:"wrap"}}>
                {Array.from({length:4},(_,i)=><SkeletonCard key={i} t={t} h={90} lines={1} style={{flex:"1 1 170px",minWidth:160}}/>)}
              </div>
              <SkeletonCard t={t} h={240} lines={4}/>
            </>}
          </div>
        </div>
      </div>;
}
