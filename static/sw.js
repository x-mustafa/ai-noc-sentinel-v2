// NOC Sentinel Service Worker — offline shell caching
const CACHE = 'noc-sentinel-v1';
const SHELL = [
  '/',
  '/static/vis-network.min.js',
  '/static/lucide.min.js',
  '/static/logo.png',
  '/static/manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Always go network-first for API calls
  if (url.pathname.startsWith('/api/')) return;
  // Cache-first for static assets, network fallback
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).catch(() => cached))
  );
});
