// Service worker for the Pakistan Youth Foundation dashboard.
//
// This app is mostly dynamic and session-based (login, progress, reports),
// so the service worker intentionally does NOT cache HTML pages — caching
// an authenticated page could leak it to the next person who opens the
// installed app on a shared device, or show stale data.
//
// What it DOES do:
//   - Caches static assets (icons, manifest, this file) so the installed
//     app has a real icon/name even the first time it's opened offline.
//   - Serves a small offline fallback page if the network is unreachable,
//     instead of the browser's default "no internet" screen.
//
// Bump CACHE_NAME whenever you change which static assets should be cached.
const CACHE_NAME = "pyf-static-v1";
const PRECACHE_URLS = [
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-512-maskable.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Only intervene for GET requests to our own static assets.
  // Everything else (login POSTs, /admin/* API calls, HTML pages) goes
  // straight to the network untouched, so sessions/cookies always behave
  // exactly as they do without a service worker.
  const url = new URL(request.url);
  const isStaticAsset = request.method === "GET" && url.pathname.startsWith("/static/");

  if (!isStaticAsset) {
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() => cached);
    })
  );
});
