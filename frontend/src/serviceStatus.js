// The one status line in the left bar's footer. It used to be a row of up to seven coloured dots with
// a word each; a person only needs to know when something is wrong, and what.
//
// `health` is the /api/health `services` object: {ollama: {status: 'ok' | 'degraded' | 'error' …}, …}.
// Names here are what the service is FOR, not its product name (those are in the Settings monitor).
const ROLES = {ollama: 'Models', codebox: 'Sandbox', searxng: 'Search', n8n: 'Automation', comfyui: 'Images', stt: 'Listening', tts: 'Speech'};
const ORDER = ['ollama', 'codebox', 'searxng', 'n8n', 'comfyui', 'stt', 'tts'];
// These four always count; the others only when this install has them (they are absent otherwise).
const ALWAYS = new Set(['ollama', 'codebox', 'searxng', 'n8n']);

const join = names => (names.length <= 1 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`);

export function serviceStatus(health) {
  const known = health && typeof health === 'object' ? health : {};
  if (!Object.keys(known).length) return {tone: 'unknown', text: 'Checking services…', down: [], slow: []};
  const present = ORDER.filter(name => known[name] !== undefined || ALWAYS.has(name));
  const down = present.filter(name => !['ok', 'degraded'].includes(known[name]?.status)).map(name => ROLES[name]);
  const slow = present.filter(name => known[name]?.status === 'degraded').map(name => ROLES[name]);
  // Up to two are named; more than that would not fit the footer, and the tooltip and monitor list them.
  if (down.length > 2) return {tone: 'down', text: `${down.length} services are down`, down, slow};
  if (down.length) return {tone: 'down', text: `${join(down)} ${down.length === 1 ? 'is' : 'are'} down`, down, slow};
  if (slow.length) return {tone: 'slow', text: `${join(slow)} ${slow.length === 1 ? 'is' : 'are'} slow`, down, slow};
  return {tone: 'ok', text: 'All services up', down, slow};
}
