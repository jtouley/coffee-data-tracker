// registry.coffee service worker (PLAN, "Offline").
//
// The shell (styles, script, fonts, an offline page) is cached at install, so the app opens without a network.
// Pages go to the network first; the last good copy of the screens used at the machine (Today, Log shot, a coffee,
// History) is kept, so they still open offline. Writes never go through here: they wait in the page's IndexedDB
// outbox until the server acknowledges them. Signing out clears the kept pages, which hold that person's data.
const SHELL = __SHELL__;
const CACHE = "shell-__VERSION__";
const PAGES = "pages";
const KEEP = [/^\/$/, /^\/log$/, /^\/bag\/new$/, /^\/coffees$/, /^\/history$/, /^\/coffee\/[\w-]+$/];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== PAGES).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (e) => {
  if (e.data === "signed-out") e.waitUntil(caches.delete(PAGES));
});

async function page(request) {
  const url = new URL(request.url);
  const keep = !url.search && KEEP.some((re) => re.test(url.pathname));
  try {
    const response = await fetch(request);
    // Only a finished, signed-in page is kept: a redirect (to sign-in) or an error isn't.
    if (keep && response.ok && !response.redirected) {
      const copy = response.clone();
      caches.open(PAGES).then((c) => c.put(url.pathname, copy));
    }
    return response;
  } catch {
    return (keep && (await caches.match(url.pathname, { cacheName: PAGES }))) || caches.match("/offline");
  }
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    // Hashed names never change content: cache first.
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  } else if (e.request.mode === "navigate") {
    e.respondWith(page(e.request));
  }
});
