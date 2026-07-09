import React from 'react';

import { IC } from '../components/icons.jsx';
import { EmptyState } from '../components/hyprChatWidgets.jsx';

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
        <div style={{padding:"14px 20px",borderBottom:`1px solid ${t.brd}28`,display:"flex",alignItems:"center",gap:8,flexShrink:0}}>
          <span style={{display:"flex",color:t.acc}}><IC.BarChart/></span>
          <div>
            <div style={{fontSize:14,fontWeight:800,letterSpacing:1,textTransform:"uppercase",color:t.acc}}>Statistics</div>
            <div style={{fontSize:10,color:t.mut,marginTop:2}}>Overall HyprChat usage from recorded chat token telemetry.</div>
          </div>
        </div>
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
              {rows.length>0?<div style={{...cardS,padding:20}}>
                <div style={{fontSize:12,fontWeight:800,marginBottom:12,color:t.mut}}>Tokens by {analyticsGroup==="persona"?"profile":analyticsGroup} {analyticsDays===0?"(all time)":`(${analyticsDays}d)`}</div>
                <div style={{display:"flex",alignItems:"flex-end",gap:2,height:210,padding:"0 4px",overflowX:"auto",overflowY:"hidden"}}>
                  {(()=>{const maxVal=Math.max(...rows.map(d=>d.total_tokens||0),1);return rows.map((d,i)=>{const h=Math.max(((d.total_tokens||0)/maxVal)*180,2);const label=d.date?d.date.slice(5):d.model||d.persona_name||"(none)";return <div key={i} style={{flex:"1 0 26px",display:"flex",flexDirection:"column",alignItems:"center",gap:4,minWidth:26}}>
                    <div style={{fontSize:8,color:t.mut,textAlign:"center",lineHeight:1}}>{(d.total_tokens||0).toLocaleString()}</div>
                    <div title={`${label}: ${(d.total_tokens||0).toLocaleString()} tokens, ${d.request_count||0} reqs`} style={{width:"80%",maxWidth:40,height:h,background:`linear-gradient(180deg,${t.acc},${t.acc}66)`,borderRadius:"4px 4px 0 0",transition:"height .3s",cursor:"pointer",minWidth:6}}/>
                    <div style={{fontSize:7,color:t.mut,textAlign:"center",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap",maxWidth:56,transform:"rotate(-30deg)",transformOrigin:"top center"}}>{label}</div>
                  </div>;});})()}
                </div>
              </div>:<EmptyState t={t} icon={<IC.BarChart/>} title="No statistics recorded yet" hint="Start chatting to record token telemetry and populate this page."/>}
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
            })():<div style={{textAlign:"center",padding:40,color:t.mut}}>Loading statistics...</div>}
          </div>
        </div>
      </div>;
}
