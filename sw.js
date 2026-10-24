const CACHE = 'fuerman-scan-v12';
const ASSETS = ['/', '/manifest.webmanifest', '/favicon.ico', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'];

async function precache() {
  const cache = await caches.open(CACHE);
  await Promise.all(ASSETS.map(async (url) => {
    try {
      const res = await fetch(new Request(url, { cache: 'reload' }));
      if (res && res.ok) await cache.put(url, res);
    } catch (_) {}
  }));
}

self.addEventListener('install', (e) => {
  e.waitUntil(precache().then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (u.origin !== self.location.origin) return;
  if (u.pathname.startsWith('/api/')) return;
  e.respondWith((async () => {
    try {
      const res = await fetch(e.request, { cache: 'no-store' });
      if (res && res.ok && res.type !== 'opaque') {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy).catch(() => {}));
      }
      return res;
    } catch (_) {
      const hit = await caches.match(e.request, { ignoreSearch: u.pathname === '/' });
      return hit || (await caches.match('/')) || Response.error();
    }
  })());
});
