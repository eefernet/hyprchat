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
  if (!r.ok) {
    const detail = data?.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map(item => {
        if (typeof item === 'string') return item;
        const field = (item?.loc || []).filter(part => part !== 'body').join('.');
        return item?.msg ? `${field ? field + ': ' : ''}${item.msg}` : '';
      }).filter(Boolean).join('; ') : '';
    throw new Error(message || `HTTP ${r.status}`);
  }
  return data;
}
