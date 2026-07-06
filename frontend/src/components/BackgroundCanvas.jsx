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
    // ── juice helpers: color mixing, glow sprites, trail fade, pointer parallax ──
    const hexRgb=(hex)=>{const n=parseInt(hex.slice(1,7),16);return[n>>16&255,n>>8&255,n&255];};
    const mix=(c1,c2,f)=>{const a=hexRgb(c1),b=hexRgb(c2);return`rgb(${a.map((v,k)=>Math.round(v+(b[k]-v)*f)).join(",")})`;};
    // Pre-rendered radial-gradient sprites replace per-frame shadowBlur (which is slow).
    // Rendered at device px, drawn at CSS px -> 1:1 through the dpr transform, so the
    // global imageSmoothingEnabled=false never resamples them.
    const sprites=new Map();
    const glow=(color,r)=>{
      const R=Math.max(2,Math.round(r)), key=color+"|"+R;
      let s=sprites.get(key); if(s)return s;
      const px=Math.max(4,Math.round(R*2*view.dpr)), c=document.createElement("canvas");
      c.width=px;c.height=px;
      const g=c.getContext("2d"), grd=g.createRadialGradient(px/2,px/2,0,px/2,px/2,px/2);
      grd.addColorStop(0,color+"cc");grd.addColorStop(.3,color+"55");grd.addColorStop(1,color+"00");
      g.fillStyle=grd;g.fillRect(0,0,px,px);
      s={img:c,size:R*2};sprites.set(key,s);return s;
    };
    const drawGlow=(color,r,x,y,alpha)=>{
      const s=glow(color,r);
      ctx.globalAlpha=alpha;
      ctx.drawImage(s.img,x-s.size/2,y-s.size/2,s.size,s.size);
    };
    // Fades toward TRANSPARENT (canvas floats over the app background — a solid fill
    // would tint the page). Same technique drawFlow already used.
    const fade=(a)=>{
      ctx.globalCompositeOperation="destination-out";
      ctx.fillStyle=`rgba(0,0,0,${a})`;
      ctx.fillRect(0,0,view.w,view.h);
      ctx.globalCompositeOperation="source-over";
    };
    // Pointer parallax: z in (0..1], z=1 = nearest layer = most shift. Applied at draw
    // coords only — particle state never mutates from the pointer.
    const par={x:0,y:0,tx:0,ty:0};
    const PAR=12;
    const onPointer=(e)=>{
      par.tx=(e.clientX/window.innerWidth-.5)*2;
      par.ty=(e.clientY/window.innerHeight-.5)*2;
    };
    const ox=(z)=>par.x*PAR*z, oy=(z)=>par.y*PAR*z;
    // ── flagship transient state (reset in seed so no stale refs survive reseeds) ──
    let pulses=[], pulseTimer=900;            // neural edge pulses
    let packets=[], packetTimer=600;          // circuit data packets
    let shoot=null, shootTimer=4000;          // stars shooting star
    let auroraBands=[], flowRamp=[];
    let circGenCounter=0;
    const makeRain=(w,h,i)=>({x:Math.random()*w,y:Math.random()*h-h*.4,c:colors[i%colors.length],a:.08+Math.random()*.16,v:1+Math.random()*2.6,drift:(Math.random()-.5)*.42,size:2+(Math.random()>.82?2:0),blocks:1+Math.floor(Math.random()*4),gap:6+Math.floor(Math.random()*8)});
    const makeFlow=(w,h,i)=>({x:Math.random()*w,y:Math.random()*h,c:colors[i%colors.length],a:.16+Math.random()*.24,s:.36+Math.random()*.8,z:.4+Math.random()*.6});
    const makeStar=(w,h,i)=>{
      const zr=Math.random(), z=zr<.5?.35:zr<.8?.6:1;
      return {x:Math.random()*w,y:Math.random()*h,c:colors[i%colors.length],a:(.24+Math.random()*.44)*(.45+z*.55),v:(.14+Math.random()*.5)*(.35+z*.65),size:z===1?1+(Math.random()>.5?1:0)+(Math.random()>.8?1:0):1,tw:Math.random()*Math.PI*2,twS:.0012+Math.random()*.0018,z};
    };
    const makePoint=(w,h,i)=>({x:Math.random()*w,y:Math.random()*h,c:colors[i%colors.length],a:.08+Math.random()*.3,v:.15+Math.random()*.9,r:1+Math.random()*3,phase:Math.random()*Math.PI*2});
    const makeNeural=(w,h,i)=>{
      const z=.35+Math.random()*.65;
      return {x:Math.random()*w,y:Math.random()*h,c:colors[i%colors.length],a:.25+z*.4,r:1.2+z*1.8,phase:Math.random()*Math.PI*2,z};
    };
    const makeCircuit=(w,h,i)=>{
      let x=Math.random()*w, y=Math.random()*h, segs=[];
      for(let j=0;j<5+Math.floor(Math.random()*5);j++){
        const len=(30+Math.random()*130)*(Math.random()>.5?1:-1);
        segs.push(Math.random()>.5?[len,0]:[0,len*.55]);
      }
      let total=0;segs.forEach(([dx,dy])=>{total+=Math.abs(dx)+Math.abs(dy);});
      const zr=Math.random();
      return {x,y,segs,total,gen:++circGenCounter,z:zr<.4?.5:zr<.75?.75:1,c:colors[i%colors.length],a:.11+Math.random()*.16,phase:Math.random()*Math.PI*2,v:.045+Math.random()*.1};
    };
    const seed=(w,h)=>{
      const area=w*h;
      pulses=[];packets=[];shoot=null;
      if(effect==="rain")particles=Array.from({length:clampCount(area/36000,24,78)},(_,i)=>makeRain(w,h,i));
      else if(effect==="flow"){
        particles=Array.from({length:clampCount(area/11000,70,240)},(_,i)=>makeFlow(w,h,i));
        flowRamp=Array.from({length:9},(_,i)=>mix(t.acc,t.acc2||t.acc,i/8));
      }
      else if(effect==="stars")particles=Array.from({length:clampCount(area/8000,90,300)},(_,i)=>makeStar(w,h,i));
      else if(effect==="circuit")particles=Array.from({length:clampCount(area/30000,25,82)},(_,i)=>makeCircuit(w,h,i));
      else if(effect==="neural")particles=Array.from({length:clampCount(area/22000,40,110)},(_,i)=>makeNeural(w,h,i));
      else if(effect==="aurora"){
        particles=[];
        auroraBands=Array.from({length:4},(_,i)=>({base:.2+i*.15,amp:24+i*10,ph:Math.random()*9,hgt:.2+Math.random()*.15,z:.3+i*.2}));
      }
      else if(["quantum","veins","solar","terminalGhost","orbital","lattice","synapse","sonar","magnetic","candle","microwave","blueprint","signal","sacred"].includes(effect))particles=Array.from({length:clampCount(area/15000,55,260)},(_,i)=>makePoint(w,h,i));
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
      sprites.clear();
      seed(view.w,view.h);
      ctx.clearRect(0,0,view.w,view.h);
    };
    const drawRain=(time,w,h,dtf)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach((p,i)=>{
        p.y+=p.v*dtf;
        p.x+=(p.drift+Math.sin(time*.001+i)*.05)*dtf;
        if(p.y>h+40||p.x<-20||p.x>w+20)Object.assign(p,makeRain(w,h,i),{y:-Math.random()*h*.35});
        const x=snap(p.x,4), y=snap(p.y,4);
        ctx.fillStyle=p.c;
        ctx.globalAlpha=p.a;
        for(let b=0;b<p.blocks;b++)ctx.fillRect(x,y-b*p.gap,p.size,p.size*2);
      });
      ctx.globalAlpha=1;
    };
    const drawFlow=(time,w,h,dtf)=>{
      fade(.05);
      ctx.globalCompositeOperation="lighter";
      particles.forEach((p,i)=>{
        const px=p.x, py=p.y;
        const n=noise(p.x*.0036+time*.000035,p.y*.0036-time*.000022);
        const ang=n*Math.PI*4;
        const sp=(0.28+p.s)*(0.4+p.z*.8)*dtf;
        const nx=p.x+Math.cos(ang)*sp;
        const ny=p.y+Math.sin(ang)*sp;
        if(nx<-8||nx>w+8||ny<-8||ny>h+8){Object.assign(p,makeFlow(w,h,i));return;}
        p.x=nx;p.y=ny;
        ctx.globalAlpha=p.a*(.45+p.z*.55);
        ctx.strokeStyle=flowRamp[Math.min(8,Math.floor(n*9))];
        ctx.lineWidth=1+p.z*.8;
        ctx.beginPath();
        ctx.moveTo(px+ox(p.z),py+oy(p.z));
        ctx.lineTo(p.x+ox(p.z),p.y+oy(p.z));
        ctx.stroke();
      });
      ctx.globalCompositeOperation="source-over";
      ctx.globalAlpha=1;
    };
    const drawAurora=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);
      ctx.globalCompositeOperation="lighter";
      const trio=[t.acc,t.acc2||t.f1||t.acc,t.f1||t.acc];
      for(let i=0;i<auroraBands.length;i++){
        const b=auroraBands[i];
        const yBase=h*b.base, bandH=h*b.hgt;
        const c1=trio[i%3], c2=trio[(i+1)%3];
        const top=[];
        for(let x=-40;x<=w+40;x+=24){
          const y=yBase+Math.sin(x*.004+time*.00019+b.ph)*b.amp+noise(x*.0018+b.ph,time*.00004+i*7)*40+oy(b.z)*.4;
          top.push([x+ox(b.z),y]);
        }
        const grd=ctx.createLinearGradient(0,yBase-b.amp,0,yBase+bandH+b.amp);
        grd.addColorStop(0,c2+"00");
        grd.addColorStop(.25,c1+"30");
        grd.addColorStop(.65,c2+"14");
        grd.addColorStop(1,c1+"00");
        ctx.fillStyle=grd;
        ctx.globalAlpha=1;
        ctx.beginPath();
        ctx.moveTo(top[0][0],top[0][1]);
        for(let k=1;k<top.length;k++)ctx.lineTo(top[k][0],top[k][1]);
        for(let k=top.length-1;k>=0;k--){
          const [x,y]=top[k];
          ctx.lineTo(x,y+bandH*(0.8+0.2*Math.sin(x*.006-time*.00013)));
        }
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha=.1;
        ctx.strokeStyle=c1;
        ctx.lineWidth=1.6;
        ctx.beginPath();
        ctx.moveTo(top[0][0],top[0][1]);
        for(let k=1;k<top.length;k++)ctx.lineTo(top[k][0],top[k][1]);
        ctx.stroke();
      }
      ctx.globalCompositeOperation="source-over";
      ctx.globalAlpha=1;
    };
    const drawStars=(time,w,h,dtf)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach((p,i)=>{
        p.y+=p.v*dtf;
        p.x+=Math.sin(time*.00045+i)*.08*dtf;
        if(p.y>h+8)Object.assign(p,makeStar(w,h,i),{y:-8});
        const x=snap(p.x+ox(p.z),2), y=snap(p.y+oy(p.z),2);
        const alpha=p.a*(.65+.35*Math.sin(time*p.twS+p.tw+i));
        if(p.size>1){
          ctx.globalCompositeOperation="lighter";
          drawGlow(p.c,2+p.size*2,x,y,alpha*.5);
          ctx.globalCompositeOperation="source-over";
        }
        ctx.fillStyle=p.c;
        ctx.globalAlpha=alpha;
        ctx.fillRect(x,y,p.size,p.size);
        if(p.size>1){
          ctx.globalAlpha=alpha*.36;
          ctx.fillRect(x-2,y,p.size+4,1);
          ctx.fillRect(x,y-2,1,p.size+4);
        }
      });
      if(!shoot){
        shootTimer-=16.667*dtf;
        if(shootTimer<=0){
          shoot={x:Math.random()*w*.6,y:Math.random()*h*.3,vx:7+Math.random()*4,vy:2.5+Math.random()*1.5,trail:[]};
          shootTimer=7000+Math.random()*7000;
        }
      }
      if(shoot){
        shoot.x+=shoot.vx*dtf;shoot.y+=shoot.vy*dtf;
        shoot.trail.push([shoot.x,shoot.y]);
        if(shoot.trail.length>12)shoot.trail.shift();
        ctx.globalCompositeOperation="lighter";
        shoot.trail.forEach(([tx,ty],k)=>drawGlow(t.acc,3,tx,ty,(k+1)/shoot.trail.length*.45));
        drawGlow(t.acc2||t.acc,5,shoot.x,shoot.y,.8);
        ctx.globalAlpha=.9;
        ctx.fillStyle=t.acc;
        ctx.fillRect(Math.round(shoot.x)-1,Math.round(shoot.y)-1,2,2);
        ctx.globalCompositeOperation="source-over";
        if(shoot.x>w+30||shoot.y>h+30)shoot=null;
      }
      ctx.globalAlpha=1;
    };
    const drawCircuit=(time,w,h,dtf)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach((p,i)=>{
        p.y+=p.v*dtf;
        if(p.y>h+160)Object.assign(p,makeCircuit(w,h,i),{y:-120});
        const pox=ox(p.z), poy=oy(p.z);
        let x=p.x, y=p.y;
        ctx.beginPath();
        ctx.moveTo(snap(x,8)+pox,snap(y,8)+poy);
        p.segs.forEach(([dx,dy])=>{x+=dx;y+=dy;ctx.lineTo(snap(x,8)+pox,snap(y,8)+poy);});
        ctx.strokeStyle=p.c;
        const pulsePhase=.6+.4*Math.sin(time*.001+p.phase);
        ctx.globalAlpha=p.a*pulsePhase;
        ctx.lineWidth=1.45;
        ctx.stroke();
        ctx.fillStyle=p.c;
        ctx.globalAlpha*=.8;
        ctx.fillRect(snap(x,8)+pox-2,snap(y,8)+poy-2,5,5);
        ctx.globalCompositeOperation="lighter";
        drawGlow(p.c,6,snap(x,8)+pox,snap(y,8)+poy,.35*pulsePhase);
        ctx.globalCompositeOperation="source-over";
      });
      packetTimer-=16.667*dtf;
      if(packetTimer<=0&&packets.length<8&&particles.length){
        const tr=particles[Math.floor(Math.random()*particles.length)];
        packets.push({tr,gen:tr.gen,d:0,v:1.2+Math.random()*1.5});
        packetTimer=400+Math.random()*500;
      }
      ctx.globalCompositeOperation="lighter";
      packets=packets.filter(pk=>{
        const tr=pk.tr;
        if(tr.gen!==pk.gen)return false;
        pk.d+=pk.v*dtf;
        if(pk.d>=tr.total)return false;
        let rem=pk.d, x=tr.x, y=tr.y;
        for(let s=0;s<tr.segs.length;s++){
          const [dx,dy]=tr.segs[s], len=Math.abs(dx)+Math.abs(dy);
          if(rem<=len){const f=len?rem/len:0;x+=dx*f;y+=dy*f;break;}
          rem-=len;x+=dx;y+=dy;
        }
        const pox=ox(tr.z), poy=oy(tr.z);
        drawGlow(tr.c,5,x+pox,y+poy,.6);
        ctx.globalAlpha=.9;
        ctx.fillStyle=tr.c;
        ctx.fillRect(Math.round(x)+pox-1,Math.round(y)+poy-1,3,3);
        return true;
      });
      ctx.globalCompositeOperation="source-over";
      ctx.globalAlpha=1;
    };
    const drawNeural=(time,w,h,dtf)=>{
      ctx.clearRect(0,0,w,h);
      particles.forEach(p=>{
        p.x+=Math.sin(time*.00035+p.phase)*.16*p.z*dtf;
        p.y+=Math.cos(time*.00028+p.phase)*.16*p.z*dtf;
      });
      for(let i=0;i<particles.length;i++)for(let j=i+1;j<particles.length;j+=3){
        const a=particles[i],b=particles[j],dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy);
        if(d<150){
          ctx.globalAlpha=(1-d/150)*.14*Math.min(a.z,b.z);
          ctx.strokeStyle=a.c;ctx.lineWidth=1;
          ctx.beginPath();ctx.moveTo(a.x+ox(a.z),a.y+oy(a.z));ctx.lineTo(b.x+ox(b.z),b.y+oy(b.z));ctx.stroke();
        }
      }
      ctx.globalCompositeOperation="lighter";
      particles.forEach(p=>{
        const x=p.x+ox(p.z), y=p.y+oy(p.z);
        drawGlow(p.c,3+p.z*4,x,y,p.a);
        ctx.globalAlpha=p.a;
        ctx.fillStyle=p.c;
        ctx.beginPath();ctx.arc(x,y,Math.min(2,p.r),0,Math.PI*2);ctx.fill();
      });
      pulseTimer-=16.667*dtf;
      if(pulseTimer<=0&&pulses.length<6&&particles.length>4){
        const a=particles[Math.floor(Math.random()*particles.length)];
        for(let j=0;j<particles.length;j+=3){
          const b=particles[j];
          if(b===a)continue;
          if(Math.hypot(a.x-b.x,a.y-b.y)<150){pulses.push({a,b,t:0,v:.6+Math.random()*.6});break;}
        }
        pulseTimer=700+Math.random()*500;
      }
      pulses=pulses.filter(pu=>{
        pu.t+=pu.v*dtf/60;
        if(pu.t>=1)return false;
        const x=pu.a.x+(pu.b.x-pu.a.x)*pu.t+ox(pu.a.z);
        const y=pu.a.y+(pu.b.y-pu.a.y)*pu.t+oy(pu.a.z);
        drawGlow(pu.a.c,10,x,y,.5);
        ctx.globalAlpha=.85;
        ctx.fillStyle=pu.a.c;
        ctx.beginPath();ctx.arc(x,y,1.6,0,Math.PI*2);ctx.fill();
        return true;
      });
      ctx.globalCompositeOperation="source-over";
      ctx.globalAlpha=1;
    };
    const drawSacred=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.save();ctx.translate(w/2+ox(.4),h/2+oy(.4));ctx.rotate(time*.00008);ctx.strokeStyle=t.acc;ctx.lineWidth=1.2;
      for(let ring=0;ring<5;ring++){const r=Math.min(w,h)*(.11+ring*.075);ctx.globalAlpha=.08+ring*.018;ctx.beginPath();ctx.arc(0,0,r,0,Math.PI*2);ctx.stroke();for(let i=0;i<6;i++){const a=i*Math.PI/3+ring*.34;ctx.beginPath();ctx.arc(Math.cos(a)*r*.82,Math.sin(a)*r*.82,r*.38,0,Math.PI*2);ctx.stroke();}}
      ctx.restore();ctx.globalAlpha=1;
    };
    const drawSignal=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);const cx=w*.22,cy=h*.72;for(let i=0;i<10;i++){const r=((time*.045+i*90)%(Math.max(w,h)*1.2));ctx.globalAlpha=Math.max(0,.16-r/(Math.max(w,h)*7));ctx.strokeStyle=colors[i%colors.length];ctx.lineWidth=1.4;ctx.beginPath();ctx.arc(cx,cy,r,-.95,-.05);ctx.stroke();}ctx.globalAlpha=1;
    };
    const drawQuantum=(time,w,h,dtf)=>{
      ctx.clearRect(0,0,w,h);particles.forEach((p,i)=>{if(Math.random()<.012){p.x=Math.random()*w;p.y=Math.random()*h;}p.x+=Math.sin(time*.001+p.phase)*.25*dtf;p.y+=Math.cos(time*.0008+p.phase)*.25*dtf;ctx.globalAlpha=p.a*(.35+.65*Math.sin(time*.006+i)**2);ctx.fillStyle=p.c;ctx.fillRect(snap(p.x,2),snap(p.y,2),p.r,p.r);});ctx.globalAlpha=1;
    };
    const drawVeins=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.slice(0,70).forEach((p,i)=>{const x=(p.x+time*.015*(i%3+1))%(w+160)-80;const y=p.y;ctx.globalAlpha=.08+p.a*.25;ctx.strokeStyle=p.c;ctx.beginPath();ctx.moveTo(x,y);for(let k=0;k<5;k++)ctx.lineTo(x+k*48,y+Math.sin(time*.0008+i+k)*34);ctx.stroke();});ctx.globalAlpha=1;
    };
    const drawSolar=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);
      ctx.globalCompositeOperation="lighter";
      for(let i=0;i<18;i++){const z=.2+(i/18)*.8;const lx=ox(z),ly=oy(z);ctx.globalAlpha=.05+(i%4)*.015;ctx.strokeStyle=colors[i%colors.length];ctx.lineWidth=1.2;ctx.beginPath();for(let x=-80;x<w+80;x+=32){const y=h*(.2+i*.035)+Math.sin(x*.01+time*.0005+i)*28;if(x===-80)ctx.moveTo(x+lx,y+ly);else ctx.lineTo(x+lx,y+ly);}ctx.stroke();}
      ctx.globalCompositeOperation="source-over";ctx.globalAlpha=1;
    };
    const drawTerminal=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.font="11px monospace";const glyphs="01AF39#{}[]<>/sys:root";particles.slice(0,90).forEach((p,i)=>{const y=(p.y+time*.018*(1+i%4))%(h+40);ctx.globalAlpha=.04+p.a*.22;ctx.fillStyle=p.c;ctx.fillText(glyphs[(i+Math.floor(time*.002))%glyphs.length],snap(p.x,12),snap(y,14));});ctx.globalAlpha=1;
    };
    const drawOrbital=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.slice(0,12).forEach((p,i)=>{const z=.3+(i%5)*.14;const cx=w*((i%4)+1)/5+ox(z),cy=h*(.24+Math.floor(i/4)*.22)+oy(z),r=34+(i%5)*18;ctx.globalAlpha=.09;ctx.strokeStyle=p.c;ctx.beginPath();ctx.ellipse(cx,cy,r,r*.42,(i%3)*.6,0,Math.PI*2);ctx.stroke();const a=time*.00035*(i%3+1)+p.phase;const bx=cx+Math.cos(a)*r,by=cy+Math.sin(a)*r*.42;ctx.globalCompositeOperation="lighter";drawGlow(p.c,4,bx,by,.4);ctx.globalCompositeOperation="source-over";ctx.globalAlpha=.42;ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(bx,by,2.2,0,Math.PI*2);ctx.fill();});ctx.globalAlpha=1;
    };
    const drawLattice=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.save();ctx.translate(ox(.3),oy(.3));ctx.strokeStyle=t.acc;ctx.lineWidth=1;for(let y=h*.18;y<h;y+=36){ctx.globalAlpha=.045+(y/h)*.08;ctx.beginPath();for(let x=0;x<=w;x+=36){const yy=y+Math.sin(x*.012+time*.0008+y*.01)*10;if(x===0)ctx.moveTo(x,yy);else ctx.lineTo(x,yy);}ctx.stroke();}for(let x=0;x<w;x+=52){ctx.globalAlpha=.04;ctx.beginPath();ctx.moveTo(x,h*.18);ctx.lineTo(w/2+(x-w/2)*1.8,h);ctx.stroke();}ctx.restore();ctx.globalAlpha=1;
    };
    const drawSynapse=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.save();ctx.translate(ox(.6),oy(.6));particles.slice(0,45).forEach((p,i)=>{const y=(p.y+time*.035*(1+i%3))%(h+80)-40;ctx.strokeStyle=p.c;ctx.globalAlpha=.08+p.a*.15;ctx.beginPath();ctx.moveTo(p.x,y);ctx.bezierCurveTo(p.x+30,y+30,p.x-20,y+70,p.x+50,y+110);ctx.stroke();const dx2=p.x+Math.sin(time*.004+i)*34,dy2=y+((time*.08+i*17)%110);ctx.globalCompositeOperation="lighter";drawGlow(p.c,5,dx2,dy2,.4);ctx.globalCompositeOperation="source-over";ctx.globalAlpha=.45;ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(dx2,dy2,2,0,Math.PI*2);ctx.fill();});ctx.restore();ctx.globalAlpha=1;
    };
    const drawSonar=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);const cx=w*.78+ox(.4),cy=h*.68+oy(.4),max=Math.max(w,h);
      ctx.globalCompositeOperation="lighter";
      drawGlow(colors[0],14,cx,cy,.25+.15*Math.sin(time*.002));
      for(let i=0;i<7;i++){const r=(time*.055+i*130)%max;ctx.globalAlpha=Math.max(0,.18-r/max*.18);ctx.strokeStyle=colors[i%colors.length];ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.stroke();}
      ctx.globalCompositeOperation="source-over";ctx.globalAlpha=1;
    };
    const drawMagnetic=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.save();ctx.translate(ox(.5),oy(.5));
      ctx.globalCompositeOperation="lighter";
      for(let i=0;i<24;i++){ctx.globalAlpha=.055;ctx.strokeStyle=colors[i%colors.length];ctx.beginPath();for(let y=-40;y<h+40;y+=28){const x=w*.5+Math.sin(y*.01+time*.0004+i)*Math.min(w,h)*(.18+i*.006);if(y===-40)ctx.moveTo(x,y);else ctx.lineTo(x,y);}ctx.stroke();}
      ctx.globalCompositeOperation="source-over";ctx.restore();ctx.globalAlpha=1;
    };
    const drawCandle=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);const grd=ctx.createRadialGradient(w*.5,h*.42,20,w*.5,h*.42,Math.max(w,h)*.7);grd.addColorStop(0,`${t.warm}20`);grd.addColorStop(1,"rgba(0,0,0,0)");ctx.fillStyle=grd;ctx.globalAlpha=.5+.1*Math.sin(time*.006);ctx.fillRect(0,0,w,h);for(let i=0;i<40;i++){ctx.globalAlpha=.015;ctx.fillStyle=i%2?t.warm:t.acc;ctx.fillRect(Math.random()*w,Math.random()*h,1,Math.random()*80);}ctx.globalAlpha=1;
    };
    const drawMicrowave=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);particles.forEach((p,i)=>{ctx.globalAlpha=.035+p.a*.12;ctx.fillStyle=colors[(i+Math.floor(time*.0004))%colors.length];ctx.fillRect(snap(p.x,3),snap(p.y,3),2,2);});ctx.globalAlpha=1;
    };
    const drawBlueprint=(time,w,h)=>{
      ctx.clearRect(0,0,w,h);ctx.save();ctx.translate(ox(.3),oy(.3));ctx.strokeStyle=t.acc;ctx.lineWidth=1;for(let i=0;i<16;i++){const x=(particles[i]?.x||0),y=(particles[i]?.y||0),ww=60+(i%5)*26,hh=30+(i%4)*18;ctx.globalAlpha=.045+(i%3)*.02;ctx.strokeRect(snap(x,16),snap(y,16),ww,hh);ctx.beginPath();ctx.moveTo(snap(x,16)-16,snap(y,16)+hh/2);ctx.lineTo(snap(x,16)+ww+16,snap(y,16)+hh/2);ctx.moveTo(snap(x,16)+ww/2,snap(y,16)-16);ctx.lineTo(snap(x,16)+ww/2,snap(y,16)+hh+16);ctx.stroke();}ctx.restore();ctx.globalAlpha=1;
    };
    let lastDraw=0, lastTime=0;
    const step=(time)=>{
      if(stopped)return;
      if(!reduced)raf=requestAnimationFrame(step);
      if(time-lastDraw<15)return;                       // ~66fps cap; skips frames on 120Hz+
      const dt=Math.min(time-(lastTime||time-16.7),100); // clamp giant deltas (tab resume)
      lastTime=time;lastDraw=time;
      const dtf=Math.min(dt/16.667,3);                  // frame factor: 1.0 at 60fps
      ctx.globalCompositeOperation="source-over";
      par.x+=(par.tx-par.x)*Math.min(1,.06*dtf);
      par.y+=(par.ty-par.y)*Math.min(1,.06*dtf);
      const w=view.w, h=view.h;
      if(effect==="rain")drawRain(time,w,h,dtf);
      else if(effect==="flow")drawFlow(time,w,h,dtf);
      else if(effect==="aurora")drawAurora(time,w,h);
      else if(effect==="stars")drawStars(time,w,h,dtf);
      else if(effect==="circuit")drawCircuit(time,w,h,dtf);
      else if(effect==="neural")drawNeural(time,w,h,dtf);
      else if(effect==="sacred")drawSacred(time,w,h);
      else if(effect==="signal")drawSignal(time,w,h);
      else if(effect==="quantum")drawQuantum(time,w,h,dtf);
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
    };
    const onVis=()=>{
      if(document.hidden)cancelAnimationFrame(raf);
      else if(!stopped&&!reduced){lastTime=0;lastDraw=0;raf=requestAnimationFrame(step);}
    };
    resize();
    window.addEventListener("resize",resize);
    let ro=null;
    if(window.ResizeObserver){ro=new ResizeObserver(resize);ro.observe(canvas);}
    if(!reduced)window.addEventListener("pointermove",onPointer,{passive:true});
    document.addEventListener("visibilitychange",onVis);
    step(performance.now());
    return ()=>{
      stopped=true;cancelAnimationFrame(raf);
      window.removeEventListener("resize",resize);
      if(ro)ro.disconnect();
      window.removeEventListener("pointermove",onPointer);
      document.removeEventListener("visibilitychange",onVis);
    };
  },[effect,t.acc,t.acc2,t.warm,t.f1]);
  if(!["rain","flow","aurora","stars","circuit","neural","sacred","signal","quantum","veins","solar","terminalGhost","orbital","lattice","synapse","sonar","magnetic","candle","microwave","blueprint"].includes(effect))return null;
  const opacity=effect==="rain"?0.58:effect==="flow"?0.8:effect==="aurora"?0.62:effect==="stars"?0.82:effect==="circuit"?0.78:effect==="candle"?0.48:effect==="microwave"?0.5:0.7;
  return <canvas ref={ref} aria-hidden="true" style={{position:"absolute",inset:0,width:"100%",height:"100%",zIndex:0,pointerEvents:"none",opacity}}/>;
}
