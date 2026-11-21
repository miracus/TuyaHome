// TuyaHome — service worker: дашборд як застосунок на телефоні.
// Сторінка й /api/* — спершу мережа, копія в кеш; немає мережі — остання копія з позначкою x-cached-at
// (дашборд показує «📴 Немає зв'язку — дані на HH:MM»). Іконки — з кешу.
const CACHE = "tuyahome-v1";

self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const r = await fetch(req);
    if (r.ok) {
      const h = new Headers(r.headers);
      h.set("x-cached-at", String(Date.now()));
      const body = await r.clone().blob();
      await cache.put(req, new Response(body, {status: r.status, statusText: r.statusText, headers: h}));
    }
    return r;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(req);
  if (hit) return hit;
  const r = await fetch(req);
  if (r.ok) await cache.put(req, r.clone());
  return r;
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) { e.respondWith(cacheFirst(e.request)); return; }
  if (url.pathname === "/" || url.pathname === "/index.html" || url.pathname.startsWith("/api/")) e.respondWith(networkFirst(e.request));
});
