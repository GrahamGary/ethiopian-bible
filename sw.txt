const CACHE_NAME = "ethiopian-bible-shell-v2.1.0";
const SHELL_URLS = [
  "./",
  "./index.html"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // Always ask the network for update metadata and update payloads.
  if (url.pathname.endsWith("/content-manifest.json") || url.pathname.endsWith(".json")) {
    event.respondWith(fetch(request));
    return;
  }

  // Navigation: network first, cached app shell when offline.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then(response => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put("./index.html", copy));
          return response;
        })
        .catch(() => caches.match("./index.html"))
    );
    return;
  }

  // Other same-origin GET resources: cache first, then network.
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request))
    );
  }
});
