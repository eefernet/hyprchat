import {useEffect,useRef} from 'react';

// Contain keyboard focus in the active modal and return it to its opener.
const scopes=[];
export default function useFocusScope(open,onClose,{inertOutside=false}={}) {
  const ref=useRef(null),close=useRef(onClose);close.current=onClose;
  useEffect(()=>{
    if(!open)return;
    const previous=document.activeElement,token={};scopes.push(token);
    const covered=[];
    if(inertOutside){
      // Inert siblings at each ancestor, never the branch containing the dialog.
      // Portalled child dialogs mounted later remain available to the top scope.
      for(let branch=ref.current;branch?.parentElement;branch=branch.parentElement){
        for(const node of branch.parentElement.children){
          if(node!==branch&&!node.inert){covered.push(node);node.inert=true;}
        }
        if(branch.parentElement===document.body)break;
      }
    }
    const nodes=()=>[...ref.current?.querySelectorAll('button:not(:disabled),[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')||[]].filter(n=>n.getClientRects().length&&!n.closest('[inert]'));
    const frame=requestAnimationFrame(()=>{if(!ref.current?.contains(document.activeElement))(nodes()[0]||ref.current)?.focus();});
    const key=e=>{
      if(scopes.at(-1)!==token)return;
      if(e.key==='Escape'&&close.current){e.preventDefault();e.stopImmediatePropagation();close.current();}
      if(e.key==='Tab'){
        const list=nodes(),first=list[0],last=list.at(-1);
        if(!list.length){e.preventDefault();return;}
        if(e.shiftKey&&(document.activeElement===first||!ref.current?.contains(document.activeElement))){e.preventDefault();last.focus();}
        else if(!e.shiftKey&&(document.activeElement===last||!ref.current?.contains(document.activeElement))){e.preventDefault();first.focus();}
      }
    };
    document.addEventListener('keydown',key,true);
    return()=>{cancelAnimationFrame(frame);scopes.splice(scopes.indexOf(token),1);document.removeEventListener('keydown',key,true);covered.forEach(node=>{node.inert=false;});if(previous?.isConnected)previous.focus({preventScroll:true});};
  },[open,inertOutside]);
  return ref;
}
