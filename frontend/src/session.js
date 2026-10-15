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
const pendingRequests = new Set();
let contextEpoch = 0;
export const hcCaptureUserContext = () => ({userId: window.__hyprchatUserId || hcStoredUserId(), session: window.__hyprchatSessionToken ?? hcStoredSession(hcStoredUserId()), epoch: contextEpoch});
export const hcSetUserContext = (id, sessionToken = null) => {
  const uid = id || "default";
  const nextSession = sessionToken === null ? window.__hyprchatSessionToken : sessionToken || "";
  if (uid !== window.__hyprchatUserId || nextSession !== window.__hyprchatSessionToken) {
    contextEpoch++;
    for (const controller of pendingRequests) controller.abort();
    pendingRequests.clear();
    window.dispatchEvent(new Event("hyprchat-profile-changing"));
  }
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
  const session = window.__hyprchatSessionToken ?? hcStoredSession(uid);
  const sep = String(path).includes("?") ? "&" : "?";
  return `${API}${path}${sep}user_id=${encodeURIComponent(uid)}${session ? `&session=${encodeURIComponent(session)}` : ""}`;
};
export const proxiedImageUrl = src => {
  if(String(src||"").startsWith("/api/"))return userScopedUrl(src);
  if(!/^https?:\/\//i.test(src||""))return src;
  let raw = src;
  if(/%25[0-9A-F]{2}/i.test(raw)){try{raw = decodeURIComponent(raw);}catch{}}
  return userScopedUrl(`/api/img-proxy?u=${encodeURIComponent(raw)}`);
};
const _hyprFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const rawUrl = typeof input === "string" ? input : (input && input.url) || "";
  const apiPath = `${API}/api/`;
  const isApi = rawUrl.startsWith("/api/") || rawUrl.startsWith(apiPath) || rawUrl.startsWith(`${window.location.origin}/api/`);
  if (!isApi) return _hyprFetch(input, init);
  const context = hcCaptureUserContext();
  const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
  if (headers.has("X-HyprChat-User") && headers.get("X-HyprChat-User") !== context.userId) {
    return Promise.reject(new DOMException("Profile changed", "AbortError"));
  }
  headers.set("X-HyprChat-User", context.userId);
  if (context.session) headers.set("X-HyprChat-Session", context.session);
  else headers.delete("X-HyprChat-Session");
  const controller = new AbortController();
  const callerSignal = init.signal || (input instanceof Request ? input.signal : null);
  const abort = () => controller.abort();
  if (callerSignal?.aborted) abort();
  else callerSignal?.addEventListener("abort", abort, {once:true});
  pendingRequests.add(controller);
  const release = () => {pendingRequests.delete(controller);callerSignal?.removeEventListener("abort", abort);};
  const assertCurrent = () => {
    if (context.epoch !== contextEpoch || controller.signal.aborted) throw new DOMException("Profile changed or request cancelled", "AbortError");
  };
  return _hyprFetch(input, {...init, headers, signal:controller.signal}).then(response => {
    assertCurrent();
    for (const method of ["json", "text", "blob", "arrayBuffer", "formData"]) {
      const consume = response[method].bind(response);
      response[method] = async (...args) => {
        try {assertCurrent();const value = await consume(...args);assertCurrent();return value;}
        finally {release();}
      };
    }
    // Stream controllers remain registered until profile change so that SSE
    // reads are cancelled too. Normal response consumers release immediately.
    return response;
  }).catch(error => {release();throw error;});
};
