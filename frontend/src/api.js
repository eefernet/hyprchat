// Shared JSON fetch helper for the Jarvis panels. Always checks r.ok and
// surfaces the backend's {detail} message — the panels' GET loaders used to
// setState(await r.json()) unchecked, so an error object crashed the render.
// The global fetch patch in session.js injects the user headers.
import { API } from './session.js';

export async function apiJson(path, opts = {}) {
  const init = { ...opts };
  if (init.body != null && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  if (init.body != null) init.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
  const r = await fetch(`${API}${path}`, init);
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}
