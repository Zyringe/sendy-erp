/*
 * Sendy ERP — Service Worker (installable-only, no offline data caching)
 *
 * Strategy:
 *   - /static/* → stale-while-revalidate (fast load, stays fresh)
 *   - everything else → network-only (no data/page caching; CSRF stays fresh)
 *
 * Cache version: bump CACHE on any static/ asset change so old entries expire.
 * Conventions doc: sendy_erp/docs/mobile-conventions.md (added in P2)
 */
const CACHE = 'sendy-v2';
const STATIC_RE = /^\/static\//;

// Install: skip waiting so the new SW takes over immediately.
self.addEventListener('install', () => self.skipWaiting());

// Activate: delete caches from old versions, then claim all open clients.
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch: ignore non-GET entirely (keeps CSRF fresh), cache /static/* with
// stale-while-revalidate, everything else falls straight through to network.
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  if (!STATIC_RE.test(url.pathname)) return;

  // Stale-while-revalidate for /static/*
  event.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(event.request).then(cached => {
        const fresh = fetch(event.request).then(resp => {
          if (resp.ok) cache.put(event.request, resp.clone());
          return resp;
        });
        return cached || fresh;
      })
    )
  );
});
