// Reject error-shaped or malformed telemetry instead of presenting an outage as zero usage.
export async function fetchAnalytics(request,days,group,signal) {
  const [summary,grouped]=await Promise.all([
    request('/api/analytics/tokens/summary',{signal}),
    request(`/api/analytics/tokens?days=${days}&group_by=${group}`,{signal}),
  ]);
  const rows=value=>Array.isArray(value)&&value.every(row=>row&&typeof row==='object'&&!Array.isArray(row)&&
    Number.isFinite(row.total_tokens)&&row.total_tokens>=0&&
    ['prompt_tokens','completion_tokens','request_count','cost_usd'].every(key=>row[key]==null||(Number.isFinite(row[key])&&row[key]>=0)));
  if(!summary||!['all_time','today','daily_30d','by_model_all','by_persona_all'].every(key=>rows(summary[key]))||!rows(grouped)) {
    throw new Error('The server returned invalid statistics. Retry the request.');
  }
  return {summary,grouped,days,group};
}
