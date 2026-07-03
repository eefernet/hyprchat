import React, { useEffect, useRef } from 'react';

export default function BackgroundCanvas({effect,t}){
  const ref=useRef(null);
  useEffect(()=>{
    const canvasEffects=["rain","flow","aurora","stars","circuit","neural","sacred","signal","quantum","veins","solar","terminalGhost","orbital","lattice","synapse","sonar","magnetic","candle","microwave","blueprint"];
    if(!canvasEffects.includes(effect))return;
    const canvas=ref.current;
    if(!canvas)return;
    const reduced=window.matchMedia&&window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const ctx=canvas.getContext("2d");
    if(!ctx)return;
    let raf=0, stopped=false, particles=[];
    let view={w:1,h:1,dpr:1};
    const colors=[t.acc,t.warm,t.f1||t.acc,t.acc2||t.acc].filter(Boolean);
    const snap=(v,s=4)=>Math.round(v/s)*s;
    const clampCount=(n,min,max)=>Math.min(max,Math.max(min,Math.floor(n)));
    const n2=(x,y)=>{
      const v=Math.sin(x*127.1+y*311.7)*43758.5453123;
      return v-Math.floor(v);
    };
    const noise=(x,y)=>{
      const xi=Math.floor(x), yi=Math.floor(y), xf=x-xi, yf=y-yi;
      const u=xf*xf*(3-2*xf), v=yf*yf*(3-2*yf);
      const a=n2(xi,yi), b=n2(xi+1,yi), c=n2(xi,yi+1), d=n2(xi+1,yi+1);
      return (a+(b-a)*u)+((c+(d-c)*u)-(a+(b-a)*u))*v;
    };
    const makeRain=(w,h,i)=>({x:Math.random()*w,y:Math.random()*h-h*.4,c:colors[i%colors.length],a:.08+Math.random()*.16,v:1+Math.random()*2.6,drift:(Math.random()-.5)*.42,size:2+(Math.random()>.82?2:0),blocks:1+Math.floor(Math.random()*4),gap:6+Math.floor(Math.random()*8)});
    const makeFlow=(w,h,i)=>({x:Math.random()*w,y:Math.random()*h,c:colors[i%colors.length],a:.16+Math.random()*.24,s:.36+Math.random()*.8});
    const makeStar=(w,h,i)=>({x:Math.random()*w,y:Math.random()*h,c:colors[i%colors.length],a:.28+Math.random()*.48,v:.18+Math.random()*.65,size:1+(Math.random()>.62?1:0)+(Math.random()>.9?1:0)});
    const makePoint=(w,h,i)=>({x:Math.random()*w,y:Math.random()*h,c:colors[i%colors.length],a:.08+Math.random()*.3,v:.15+Math.random()*.9,r:1+Math.random()*3,phase:Math.random()*Math.PI*2});
    const makeCircuit=(w,h,i)=>{
      let x=Math.random()*w, y=Math.random()*h, segs=[];
      for(let j=0;j<5+Math.floor(Math.random()*5);j++){
        const len=(30+Math.random()*130)*(Math.random()>.5?1:-1);
        segs.push(Math.random()>.5?[len,0]:[0,len*.55]);
      }
      return {x,y,segs,c:colors[i%colors.length],a:.11+Math.random()*.16,phase:Math.random()*Math.PI*2,v:.045+Math.random()*.1};
    };
    const seed=(w,h)=>{
      const area=w*h;
      if(effect==="rain")particles=Array.from({length:clampCount(area/36000,24,78)},(_,i)=>makeRain(w,h,i));
      else if(effect==="flow")particles=Array.from({length:clampCount(area/11000,70,240)},(_,i)=>makeFlow(w,h,i));
      else if(effect==="stars")particles=Array.from({length:clampCount(area/8000,90,300)},(_,i)=>makeStar(w,h,i));
      else if(effect==="circuit")particles=Array.from({length:clampCount(area/30000,25,82)},(_,i)=>makeCircuit(w,h,i));
      else if(["neural","quantum","veins","solar","terminalGhost","orbital","lattice","synapse","sonar","magnetic","candle","microwave","blueprint","signal","sacred"].includes(effect))particles=Array.from({length:clampCount(area/15000,55,260)},(_,i)=>makePoint(w,h,i));
      else particles=[];
    };
    const resize=()=>{
      const rect=canvas.getBoundingClientRect();
      const dpr=Math.min(window.devicePixelRatio||1,2);
      view={w:Math.max(1,rect.width),h:Math.max(1,rect.height),dpr};
      canvas.width=Math.max(1,Math.floor(view.w*dpr));
      canvas.height=Math.max(1,Math.floor(view.h*dpr));
      ctx.setTransform(dpr,0,0,dpr,0,0);
      ctx.imageSmoothingEnabled=false;
      seed(view.w,view.h);
      ctx.clearRect(0,0,view.w,view.h);
    };
    const drawRain=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach((p,i)=>{
        p.y+=p.v;
        p.x+=p.drift+Math.sin(time*.001+i)*.05;
        if(p.y>h+40||p.x<-20||p.x>w+20)Object.assign(p,makeRain(w,h,i),{y:-Math.random()*h*.35});
        const x=snap(p.x,4), y=snap(p.y,4);
        ctx.fillStyle=p.c;
        ctx.globalAlpha=p.a;
        for(let b=0;b<p.blocks;b++)ctx.fillRect(x,y-b*p.gap,p.size,p.size*2);
      });
      ctx.globalAlpha=1;
    };
    const drawFlow=(time,w,h)=>{
      ctx.globalCompositeOperation="destination-out";
      ctx.fillStyle="rgba(0,0,0,.12)";
      ctx.fillRect(0,0,w,h);
      ctx.globalCompositeOperation="source-over";
      particles.forEach((p,i)=>{
        const px=p.x, py=p.y;
        const ang=noise(p.x*.0036+time*.000035,p.y*.0036-time*.000022)*Math.PI*4;
        const nx=p.x+Math.cos(ang)*(0.28+p.s);
        const ny=p.y+Math.sin(ang)*(0.28+p.s);
        if(nx<-8||nx>w+8||ny<-8||ny>h+8){Object.assign(p,makeFlow(w,h,i));return;}
        p.x=nx;p.y=ny;
        ctx.globalAlpha=p.a;
        ctx.strokeStyle=p.c;
        ctx.lineWidth=1.35;
        ctx.beginPath();
        ctx.moveTo(px,py);
        ctx.lineTo(p.x,p.y);
        ctx.stroke();
      });
      ctx.globalAlpha=1;
    };
    const drawAurora=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);
      for(let i=0;i<7;i++){
        const yBase=h*(.16+i*.12);
        ctx.beginPath();
        for(let x=-80;x<=w+80;x+=48){
          const y=yBase+Math.sin(x*.006+time*.00024+i)*32+Math.sin(x*.017-time*.00018+i*2)*12;
          if(x===-80)ctx.moveTo(x,y);else ctx.lineTo(x,y);
        }
        ctx.strokeStyle=colors[i%colors.length];
        ctx.globalAlpha=.06+i*.011;
        ctx.lineWidth=1.3+(i%3)*.55;
        ctx.stroke();
      }
      ctx.globalAlpha=1;
    };
    const drawStars=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach((p,i)=>{
        p.y+=p.v;
        p.x+=Math.sin(time*.00045+i)*.08;
        if(p.y>h+8)Object.assign(p,makeStar(w,h,i),{y:-8});
        ctx.fillStyle=p.c;
        ctx.globalAlpha=p.a*(.72+.28*Math.sin(time*.002+i));
        const x=snap(p.x,2), y=snap(p.y,2);
        ctx.fillRect(x,y,p.size,p.size);
        if(p.size>1){
          ctx.globalAlpha*=.36;
          ctx.fillRect(x-2,y, p.size+4, 1);
          ctx.fillRect(x,y-2, 1, p.size+4);
        }
      });
      ctx.globalAlpha=1;
    };
    const drawCircuit=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach((p,i)=>{
        p.y+=p.v;
        if(p.y>h+160)Object.assign(p,makeCircuit(w,h,i),{y:-120});
        let x=p.x, y=p.y;
        ctx.beginPath();
        ctx.moveTo(snap(x,8),snap(y,8));
        p.segs.forEach(([dx,dy])=>{x+=dx;y+=dy;ctx.lineTo(snap(x,8),snap(y,8));});
        ctx.strokeStyle=p.c;
        ctx.globalAlpha=p.a*(.6+.4*Math.sin(time*.001+p.phase));
        ctx.lineWidth=1.45;
        ctx.stroke();
        ctx.fillStyle=p.c;
        ctx.globalAlpha*=.8;
        ctx.fillRect(snap(x,8)-2,snap(y,8)-2,5,5);
      });
      ctx.globalAlpha=1;
    };
    const drawNeural=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach((p,i)=>{p.x+=Math.sin(time*.00035+p.phase)*.16;p.y+=Math.cos(time*.00028+p.phase)*.16;});
      for(let i=0;i<particles.length;i++)for(let j=i+1;j<particles.length;j+=3){
        const a=particles[i],b=particles[j],dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy);
        if(d<145){ctx.globalAlpha=(1-d/145)*.12;ctx.strokeStyle=a.c;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
      }
      particles.forEach(p=>{ctx.globalAlpha=p.a;ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);ctx.fill();});ctx.globalAlpha=1;
    };
    const drawSacred=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.save();ctx.translate(w/2,h/2);ctx.rotate(time*.00008);ctx.strokeStyle=t.acc;ctx.lineWidth=1.2;
      for(let ring=0;ring<5;ring++){const r=Math.min(w,h)*(.11+ring*.075);ctx.globalAlpha=.08+ring*.018;ctx.beginPath();ctx.arc(0,0,r,0,Math.PI*2);ctx.stroke();for(let i=0;i<6;i++){const a=i*Math.PI/3+ring*.34;ctx.beginPath();ctx.arc(Math.cos(a)*r*.82,Math.sin(a)*r*.82,r*.38,0,Math.PI*2);ctx.stroke();}}
      ctx.restore();ctx.globalAlpha=1;
    };
    const drawSignal=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);const cx=w*.22,cy=h*.72;for(let i=0;i<10;i++){const r=((time*.045+i*90)%(Math.max(w,h)*1.2));ctx.globalAlpha=Math.max(0,.16-r/(Math.max(w,h)*7));ctx.strokeStyle=colors[i%colors.length];ctx.lineWidth=1.4;ctx.beginPath();ctx.arc(cx,cy,r,-.95,-.05);ctx.stroke();}ctx.globalAlpha=1;
    };
    const drawQuantum=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.forEach((p,i)=>{if(Math.random()<.012){p.x=Math.random()*w;p.y=Math.random()*h;}p.x+=Math.sin(time*.001+p.phase)*.25;p.y+=Math.cos(time*.0008+p.phase)*.25;ctx.globalAlpha=p.a*(.35+.65*Math.sin(time*.006+i)**2);ctx.fillStyle=p.c;ctx.fillRect(snap(p.x,2),snap(p.y,2),p.r,p.r);});ctx.globalAlpha=1;
    };
    const drawVeins=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.slice(0,70).forEach((p,i)=>{const x=(p.x+time*.015*(i%3+1))%(w+160)-80;const y=p.y;ctx.globalAlpha=.08+p.a*.25;ctx.strokeStyle=p.c;ctx.beginPath();ctx.moveTo(x,y);for(let k=0;k<5;k++)ctx.lineTo(x+k*48,y+Math.sin(time*.0008+i+k)*34);ctx.stroke();});ctx.globalAlpha=1;
    };
    const drawSolar=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);for(let i=0;i<18;i++){ctx.globalAlpha=.05+(i%4)*.015;ctx.strokeStyle=colors[i%colors.length];ctx.lineWidth=1.2;ctx.beginPath();for(let x=-80;x<w+80;x+=32){const y=h*(.2+i*.035)+Math.sin(x*.01+time*.0005+i)*28;if(x===-80)ctx.moveTo(x,y);else ctx.lineTo(x,y);}ctx.stroke();}ctx.globalAlpha=1;
    };
    const drawTerminal=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.font="11px monospace";const glyphs="01AF39#{}[]<>/sys:root";particles.slice(0,90).forEach((p,i)=>{const y=(p.y+time*.018*(1+i%4))%(h+40);ctx.globalAlpha=.04+p.a*.22;ctx.fillStyle=p.c;ctx.fillText(glyphs[(i+Math.floor(time*.002))%glyphs.length],snap(p.x,12),snap(y,14));});ctx.globalAlpha=1;
    };
    const drawOrbital=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.slice(0,12).forEach((p,i)=>{const cx=w*((i%4)+1)/5,cy=h*(.24+Math.floor(i/4)*.22),r=34+(i%5)*18;ctx.globalAlpha=.09;ctx.strokeStyle=p.c;ctx.beginPath();ctx.ellipse(cx,cy,r,r*.42,(i%3)*.6,0,Math.PI*2);ctx.stroke();const a=time*.00035*(i%3+1)+p.phase;ctx.globalAlpha=.42;ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(cx+Math.cos(a)*r,cy+Math.sin(a)*r*.42,2.2,0,Math.PI*2);ctx.fill();});ctx.globalAlpha=1;
    };
    const drawLattice=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.strokeStyle=t.acc;ctx.lineWidth=1;for(let y=h*.18;y<h;y+=36){ctx.globalAlpha=.045+(y/h)*.08;ctx.beginPath();for(let x=0;x<=w;x+=36){const yy=y+Math.sin(x*.012+time*.0008+y*.01)*10;if(x===0)ctx.moveTo(x,yy);else ctx.lineTo(x,yy);}ctx.stroke();}for(let x=0;x<w;x+=52){ctx.globalAlpha=.04;ctx.beginPath();ctx.moveTo(x,h*.18);ctx.lineTo(w/2+(x-w/2)*1.8,h);ctx.stroke();}ctx.globalAlpha=1;
    };
    const drawSynapse=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.slice(0,45).forEach((p,i)=>{const y=(p.y+time*.035*(1+i%3))%(h+80)-40;ctx.strokeStyle=p.c;ctx.globalAlpha=.08+p.a*.15;ctx.beginPath();ctx.moveTo(p.x,y);ctx.bezierCurveTo(p.x+30,y+30,p.x-20,y+70,p.x+50,y+110);ctx.stroke();ctx.globalAlpha=.45;ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(p.x+Math.sin(time*.004+i)*34,y+((time*.08+i*17)%110),2,0,Math.PI*2);ctx.fill();});ctx.globalAlpha=1;
    };
    const drawSonar=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);const cx=w*.78,cy=h*.68,max=Math.max(w,h);for(let i=0;i<7;i++){const r=(time*.055+i*130)%max;ctx.globalAlpha=Math.max(0,.18-r/max*.18);ctx.strokeStyle=colors[i%colors.length];ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.stroke();}ctx.globalAlpha=1;
    };
    const drawMagnetic=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);for(let i=0;i<24;i++){ctx.globalAlpha=.055;ctx.strokeStyle=colors[i%colors.length];ctx.beginPath();for(let y=-40;y<h+40;y+=28){const x=w*.5+Math.sin(y*.01+time*.0004+i)*Math.min(w,h)*(.18+i*.006);if(y===-40)ctx.moveTo(x,y);else ctx.lineTo(x,y);}ctx.stroke();}ctx.globalAlpha=1;
    };
    const drawCandle=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);const grd=ctx.createRadialGradient(w*.5,h*.42,20,w*.5,h*.42,Math.max(w,h)*.7);grd.addColorStop(0,`${t.warm}20`);grd.addColorStop(1,"rgba(0,0,0,0)");ctx.fillStyle=grd;ctx.globalAlpha=.5+.1*Math.sin(time*.006);ctx.fillRect(0,0,w,h);for(let i=0;i<40;i++){ctx.globalAlpha=.015;ctx.fillStyle=i%2?t.warm:t.acc;ctx.fillRect(Math.random()*w,Math.random()*h,1,Math.random()*80);}ctx.globalAlpha=1;
    };
    const drawMicrowave=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.forEach((p,i)=>{ctx.globalAlpha=.035+p.a*.12;ctx.fillStyle=colors[(i+Math.floor(time*.0004))%colors.length];ctx.fillRect(snap(p.x,3),snap(p.y,3),2,2);});ctx.globalAlpha=1;
    };
    const drawBlueprint=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.strokeStyle=t.acc;ctx.lineWidth=1;for(let i=0;i<16;i++){const x=(particles[i]?.x||0),y=(particles[i]?.y||0),ww=60+(i%5)*26,hh=30+(i%4)*18;ctx.globalAlpha=.045+(i%3)*.02;ctx.strokeRect(snap(x,16),snap(y,16),ww,hh);ctx.beginPath();ctx.moveTo(snap(x,16)-16,snap(y,16)+hh/2);ctx.lineTo(snap(x,16)+ww+16,snap(y,16)+hh/2);ctx.moveTo(snap(x,16)+ww/2,snap(y,16)-16);ctx.lineTo(snap(x,16)+ww/2,snap(y,16)+hh+16);ctx.stroke();}ctx.globalAlpha=1;
    };
    const step=(time)=>{
      if(stopped)return;
      const w=view.w, h=view.h;
      if(effect==="rain")drawRain(time,w,h);
      else if(effect==="flow")drawFlow(time,w,h);
      else if(effect==="aurora")drawAurora(time,w,h);
      else if(effect==="stars")drawStars(time,w,h);
      else if(effect==="circuit")drawCircuit(time,w,h);
      else if(effect==="neural")drawNeural(time,w,h);
      else if(effect==="sacred")drawSacred(time,w,h);
      else if(effect==="signal")drawSignal(time,w,h);
      else if(effect==="quantum")drawQuantum(time,w,h);
      else if(effect==="veins")drawVeins(time,w,h);
      else if(effect==="solar")drawSolar(time,w,h);
      else if(effect==="terminalGhost")drawTerminal(time,w,h);
      else if(effect==="orbital")drawOrbital(time,w,h);
      else if(effect==="lattice")drawLattice(time,w,h);
      else if(effect==="synapse")drawSynapse(time,w,h);
      else if(effect==="sonar")drawSonar(time,w,h);
      else if(effect==="magnetic")drawMagnetic(time,w,h);
      else if(effect==="candle")drawCandle(time,w,h);
      else if(effect==="microwave")drawMicrowave(time,w,h);
      else if(effect==="blueprint")drawBlueprint(time,w,h);
      if(!reduced)raf=requestAnimationFrame(step);
    };
    resize();
    window.addEventListener("resize",resize);
    let ro=null;
    if(window.ResizeObserver){ro=new ResizeObserver(resize);ro.observe(canvas);}
    step(performance.now());
    return ()=>{stopped=true;cancelAnimationFrame(raf);window.removeEventListener("resize",resize);if(ro)ro.disconnect();};
  },[effect,t.acc,t.acc2,t.warm,t.f1]);
  if(!["rain","flow","aurora","stars","circuit","neural","sacred","signal","quantum","veins","solar","terminalGhost","orbital","lattice","synapse","sonar","magnetic","candle","microwave","blueprint"].includes(effect))return null;
  const opacity=effect==="rain"?0.58:effect==="flow"?0.86:effect==="aurora"?0.66:effect==="stars"?0.82:effect==="circuit"?0.78:effect==="candle"?0.48:effect==="microwave"?0.5:0.7;
  return <canvas ref={ref} aria-hidden="true" style={{position:"absolute",inset:0,width:"100%",height:"100%",zIndex:0,pointerEvents:"none",opacity}}/>;
}
