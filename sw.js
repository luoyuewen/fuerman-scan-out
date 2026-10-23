const CACHE = 'fuerman-scan-v9';
const ASSETS = ['/', '/index.html', '/manifest.webmanifest', '/favicon.ico', '/favicon.ico?v=9', '/icon-192.png', '/icon-192.png?v=9', '/icon-512.png', '/icon-512.png?v=9', '/apple-touch-icon.png', '/apple-touch-icon.png?v=9'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (u.origin !== self.location.origin) return;
  if (u.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return res;
    }).catch(() => caches.match(e.request).then((r) => r || caches.match('/')))
  );
});
