export function taskBody(form){
  const keys={once:['run_at'],daily:['time'],weekly:['time','weekday'],monthly:['time','day'],cron:['cron'],event:[],webhook:[]};
  const schedule_json={};
  for(const key of keys[form.schedule_kind]||[])if(form[key]!==''&&form[key]!=null)schedule_json[key]=['weekday','day'].includes(key)?Number(form[key]):form[key];
  const body={title:form.title,prompt:form.prompt,schedule_kind:form.schedule_kind,schedule_json,
    timezone:form.timezone||'',model:form.model||'',tool_ids:form.tool_ids||[],workspace_id:form.workspace_id||'',
    conversation_id:form.conversation_id||'',delivery_json:{...(form.delivery||{}),conversation:!!form.conversation_id||!!form.workspace_id,notify:!!form.notify}};
  if(['llm','research'].includes(form.task_type))body.task_type=form.task_type;
  if(form.schedule_kind==='event')body.event_trigger_json={event:form.event,every:Math.max(1,Number(form.every)||1)};
  return body;
}
