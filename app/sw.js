// registry.coffee service worker: the shell (styles, script, fonts and an offline page) comes from the cache, so
// the app opens and its outbox keeps working without a network. Pages and API calls always go to the network.
//
// Hashed files never change, so they live in one cache that a new version adds to rather than replaces: a page
// still open from the last version (or kept for offline use) keeps finding its own styles and script.
const SHELL = __SHELL__;
const STATIC = "static";
const CACHE = "shell-__VERSION__"; // the unhashed shell (the offline page) for this version

self.addEventListener("install", (e) => {
  const hashed = SHELL.filter((u) => u.startsWith("/static/"));
  e.waitUntil(
    Promise.all([
      caches.open(STATIC).then((c) => c.addAll(hashed)),
      caches.open(CACHE).then((c) => c.addAll(SHELL.filter((u) => !hashed.includes(u)))),
    ]).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("shell-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function hashed(request) {
  const hit = await caches.match(request, { cacheName: STATIC });
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) {
    const copy = response.clone();
    await caches.open(STATIC).then((c) => c.put(request, copy));
  }
  return response;
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(hashed(e.request)); // cache first, and kept across versions
  } else if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/offline")));
  }
});
