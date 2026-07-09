/* HyprChat service worker — minimal, network-first.
 *
 * Purpose: PWA installability + a cached app shell so the UI still opens
 * (read-only) when the server is briefly unreachable. It deliberately does
 * NOT cache or intercept /api/* — chat streaming (SSE), uploads, and all
 * dynamic data always hit the network directly.
 *
 * Cache strategy: network-first for same-origin static files (shell, hashed
 * assets, icons, manifest). A fresh response replaces the cached copy, so
 * stale-bundle bugs can't outlive one successful load. Hashed asset names
 * change every build; old entries are dropped whenever the SW updates.
 */
const CACHE = "hyprchat-shell-v1";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // never touch API/SSE traffic

  const cacheable =
    req.mode === "navigate" ||
    url.pathname.startsWith("/assets/") ||
    url.pathname.startsWith("/icons/") ||
    url.pathname.endsWith("/manifest.json") ||
    url.pathname.endsWith("/index.html") ||
    url.pathname === "/";
  if (!cacheable) return;

  event.respondWith((async () => {
    try {
      const resp = await fetch(req);
      if (resp && resp.ok) {
        const cache = await caches.open(CACHE);
        cache.put(req, resp.clone());
      }
      return resp;
    } catch (err) {
      const cached = await caches.match(req, { ignoreSearch: req.mode === "navigate" });
      if (cached) return cached;
      if (req.mode === "navigate") {
        const shell = await caches.match("/") || await caches.match("/index.html");
        if (shell) return shell;
      }
      throw err;
    }
  })());
});
