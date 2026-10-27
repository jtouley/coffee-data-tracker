// registry.coffee service worker: the shell (styles, script, fonts and an offline page) comes from the cache, so
// the app opens and its outbox keeps working without a network. Pages and API calls always go to the network.
const SHELL = __SHELL__;
const CACHE = "shell-__VERSION__";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    // Hashed names never change content: cache first.
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  } else if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/offline")));
  }
});
