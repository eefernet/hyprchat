export const terminalJobStates=new Set(['completed','cancelled','blocked','waiting_for_input']);

export function jobBlockerMessage(value){
  const message=String(value||'');
  if(message.includes('Model-call allowance exhausted'))return 'The model-call allowance is used up. Continue from checkpoint for another allowance, or adjust it in Settings → Daedalus.';
  if(message.includes('Execution allowance exhausted'))return 'The execution allowance is used up. Continue from checkpoint for more time, or adjust it in Settings → Daedalus.';
  return message.split('\n\nConversation logs are stored at:')[0];
}

export function applyJobEvent(job,event,lastSequence=0){
  if(!event||event.seq<=lastSequence)return {job,lastSequence};
  return {job:{...job,...event.data},lastSequence:event.seq};
}

export function consumeSseFrames(buffer){
  const frames=buffer.replace(/\r\n/g,'\n').split('\n\n');
  const remainder=frames.pop();
  const events=[];
  for(const frame of frames){
    const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
    if(data){try{events.push(JSON.parse(data));}catch{}}
  }
  return {events,remainder};
}
