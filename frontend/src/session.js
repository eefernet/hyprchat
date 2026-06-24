// API/session helpers for the HyprChat SPA.
export const API = window.HYPRCHAT_API || "";
export const HC_USER_KEY = "hc-current-user";
export const HC_SESSION_PREFIX = "hc-user-session-";
export const hcSessionKey = id => `${HC_SESSION_PREFIX}${id || "default"}`;
export const hcStoredUserId = () => {
  try { return localStorage.getItem(HC_USER_KEY) || "default"; } catch { return "default"; }
};
export const hcStoredSession = id => {
  try { return localStorage.getItem(hcSessionKey(id)) || ""; } catch { return ""; }
};
export const hcSetUserContext = (id, sessionToken = null) => {
  const uid = id || "default";
  window.__hyprchatUserId = uid;
  if (sessionToken !== null) window.__hyprchatSessionToken = sessionToken || "";
};
hcSetUserContext(hcStoredUserId(), hcStoredSession(hcStoredUserId()));
export const dismissHyprLoader = () => {
  const el = document.getElementById("hc-loading");
  if (!el || el.classList.contains("hc-dismiss")) return;
  el.classList.add("hc-dismiss");
  setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 800);
};
export const userScopedUrl = path => {
  const uid = window.__hyprchatUserId || hcStoredUserId();
  const session = window.__hyprchatSessionToken || hcStoredSession(uid);
  const sep = String(path).includes("?") ? "&" : "?";
  return `${API}${path}${sep}user_id=${encodeURIComponent(uid)}${session ? `&session=${encodeURIComponent(session)}` : ""}`;
};
export const proxiedImageUrl = src => {
  if(!/^https?:\/\//i.test(src||""))return src;
  let raw = src;
  if(/%25[0-9A-F]{2}/i.test(raw)){try{raw = decodeURIComponent(raw);}catch{}}
  return `${API}/api/img-proxy?u=${encodeURIComponent(raw)}`;
};
const _hyprFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const rawUrl = typeof input === "string" ? input : (input && input.url) || "";
  const apiPath = `${API}/api/`;
  const isApi = rawUrl.startsWith("/api/") || rawUrl.startsWith(apiPath) || rawUrl.startsWith(`${window.location.origin}/api/`);
  if (!isApi) return _hyprFetch(input, init);
  const uid = window.__hyprchatUserId || hcStoredUserId();
  const session = window.__hyprchatSessionToken || hcStoredSession(uid);
  const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
  headers.set("X-HyprChat-User", uid);
  if (session) headers.set("X-HyprChat-Session", session);
  return _hyprFetch(input, {...init, headers});
};
