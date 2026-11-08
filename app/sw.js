// registry.coffee service worker (PLAN, "Offline").
//
// The shell (styles, script, fonts, an offline page) is cached at install, so the app opens without a network.
// Pages go to the network first; the last good copy of the screens used at the machine (Today, Log shot, New bag,
// Coffees, a coffee, History) is kept, so they still open offline, and so are the bag photos they show (up to
// PHOTO_LIMIT, the oldest dropped first). Writes never go through here: they wait in the
// page's IndexedDB outbox until the server acknowledges them. Signing out clears the kept pages, which hold that
// person's data.
//
// Hashed files never change, so they live in one cache that a new version adds to rather than replaces: a page
// still open from the last version, or kept for offline use, keeps finding its own styles and script.
const SHELL = __SHELL__;
const STATIC = "static";
const CACHE = "shell-__VERSION__"; // the unhashed shell (the offline page) for this version
const PAGES = "pages";
const PHOTO = /^\/photos\/[\w-]+$/;
const PHOTO_LIMIT = 60;
const KEEP = [/^\/$/, /^\/log$/, /^\/bag\/new$/, /^\/coffees$/, /^\/history$/, /^\/coffee\/[\w-]+$/, /^\/setup$/];

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

// Bumped by each sign-out: a photo fetched before it must not put the kept pages back afterwards.
let generation = 0;

self.addEventListener("message", (e) => {
  if (e.data === "signed-out") {
    generation += 1;
    e.waitUntil(caches.delete(PAGES));
  }
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

async function page(request) {
  const url = new URL(request.url);
  const keep = !url.search && KEEP.some((re) => re.test(url.pathname));
  try {
    const response = await fetch(request);
    // A selected bag changes both pages, so their kept copies go, but only once the switch succeeded (a redirect,
    // opaque to a navigation): offline or refused (a bag finished on another phone), the old copies still open.
    if (/^\/bag\/[\w-]+\/use$/.test(url.pathname) && (response.ok || response.type === "opaqueredirect")) {
      const pages = await caches.open(PAGES);
      await Promise.all([pages.delete("/"), pages.delete("/log")]);
    }
    // Only a finished, signed-in page is kept: a redirect (to sign-in) or an error isn't.
    if (keep && response.ok && !response.redirected) {
      const copy = response.clone();
      await caches.open(PAGES).then((c) => c.put(url.pathname, copy));
    }
    return response;
  } catch {
    return (keep && (await caches.match(url.pathname, { cacheName: PAGES }))) || caches.match("/offline");
  }
}

// A bag photo: from the network (the app sends it no-store), kept with the pages so a kept screen shows it offline.
// It goes with them on sign-out. Only an image is kept: signed out, the app redirects to the sign-in page, which
// fetch follows to a 200 that must not replace the photo.
async function keepPhoto(path, response, fetchedIn) {
  if (generation !== fetchedIn) return; // signed out meanwhile: the photo was the last person's
  const pages = await caches.open(PAGES);
  if (generation !== fetchedIn) return;
  await pages.put(path, response);
  if (generation !== fetchedIn) return void (await pages.delete(path)); // signed out during the put
  const kept = (await pages.keys()).filter((r) => PHOTO.test(new URL(r.url).pathname));
  await Promise.all(kept.slice(0, Math.max(0, kept.length - PHOTO_LIMIT)).map((r) => pages.delete(r)));
}

async function photo(e) {
  const url = new URL(e.request.url);
  const fetchedIn = generation;
  let response;
  try {
    response = await fetch(e.request);
  } catch {
    return (await caches.match(url.pathname, { cacheName: PAGES })) || Response.error();
  }
  const image = (response.headers.get("content-type") || "").startsWith("image/");
  if (response.ok && !response.redirected && image) {
    // Kept after answering, on its own: a full storage (QuotaExceededError) never costs the photo just fetched.
    e.waitUntil(keepPhoto(url.pathname, response.clone(), fetchedIn).catch(() => { /* not kept: still shown online */ }));
  }
  return response;
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(hashed(e.request)); // cache first, and kept across versions
  } else if (e.request.mode === "navigate") {
    e.respondWith(page(e.request));
  } else if (PHOTO.test(url.pathname)) {
    e.respondWith(photo(e));
  }
});
