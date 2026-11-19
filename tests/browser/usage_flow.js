// The usage counting in Chromium (tests/test_usage_browser.py). Site: landing, catalog, privacy, terms, bot; app: a
// coffee's Shop link and the bag screen's catalog search. Each page at 390 px and desktop, light and dark: nothing is
// sent while it loads, axe, no sideways scroll, a screenshot, and (site) one page_view when it is hidden. Then: the
// catalog's search, filter and click-out events and what they carry; utm_* and referrer sources; Global Privacy
// Control and Do Not Track send nothing; a slow and a failing collector never hold a page or a click up. A cookie is
// set on the collector's host, and no request to it may carry one (or any credentials). The collector is a local
// HTTPS server the test runs (Chromium maps the collector's host to it): a page's last events leave as it unloads, which
// in-browser routing can't see, so the server records what really arrives, headers included. Any other request off
// 127.0.0.1 fails the run, except a navigation to a store, which gets a stub page.
// Usage: node usage_flow.js <site url> <app url> <screenshot dir> <app session cookie as JSON> <release build id>
//          <collector port> <collector control url> <collector origin, as config/usage.toml would hold it>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [site, app, out, cookieJson, build, port, control, origin] = process.argv.slice(2);
const SESSION = JSON.parse(cookieJson);
const COLLECTOR = `${origin}/e`, HOST = new URL(origin).hostname;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let AXE = null;
try { AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"); } catch (e) {}
const VIEWPORTS = { 390: { width: 390, height: 844 }, desktop: { width: 1280, height: 900 } };
const errors = [];

// The collector's mode, which also forgets what it received: "ok" (204), "slow" (answers after 10 s) or "down"
// (drops the connection unanswered). seen(): every request it received since, with its headers and events.
const mode = async (m) => { await fetch(`${control}/mode/${m}`, { method: "POST" }); };
const seen = async () => (await fetch(`${control}/seen`)).json();

// init: a script run before the page's own (Global Privacy Control, Do Not Track).
async function open(browser, { viewport = VIEWPORTS[390], scheme = "light", collector = "ok", init } = {}) {
  await mode(collector);
  // The collector's certificate is the test's own, for its host.
  const context = await browser.newContext({ viewport, colorScheme: scheme, ignoreHTTPSErrors: true });
  const appHost = new URL(app).hostname;
  await context.addCookies([
    { name: "session", value: "must-not-be-sent", domain: HOST, path: "/", secure: true, sameSite: "None" },
    { name: SESSION.name, value: SESSION.value, domain: appHost, path: "/" },
  ]);
  if (init) await context.addInitScript(init);
  await context.route("**/*", async (route) => {
    const req = route.request(), url = new URL(req.url());
    if (req.url() === COLLECTOR || url.hostname === "127.0.0.1") return route.continue();
    if (req.isNavigationRequest()) return route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><title>Store</title><p>A store</p>" });
    errors.push(`outside request: ${url}`);
    return route.abort();
  });
  const page = await context.newPage();
  context.on("page", (p) => p.on("pageerror", (e) => errors.push(e.message)));
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
  return { context, page };
}

const settled = (page) => page.waitForFunction(() => !/^Loading/.test(document.getElementById("count")?.textContent ?? ""));
const until = async (fn, what, ms = 3000) => {
  const end = Date.now() + ms;
  while (!(await fn())) { if (Date.now() > end) throw new Error(`timed out waiting for ${what}`); await sleep(50); }
};
const count = async () => (await seen()).length;
// Hide the page first, so nothing it still holds arrives during the next check.
const done = async (page, context) => { await page.goto("about:blank").catch(() => {}); await sleep(300); await context.close(); };
const events = (sent) => sent.flatMap((s) => s.events);
// Every request to the collector: a POST of text/plain JSON, no cookie, no Authorization, at most 20 events.
function clean(sent) {
  for (const s of sent) {
    assert.equal(s.method, "POST");
    assert.equal(s.cookie, null, "a request to the collector carried a cookie");
    assert.ok(s.origin === new URL(site).origin || s.origin === new URL(app).origin, `Origin ${s.origin}`);  // never null
    assert.deepEqual(s.problems, [], "the collector's schema");
    assert.equal(s.auth, null, "a request to the collector carried credentials");
    assert.match(s.type, /^text\/plain/);
    assert.ok(s.events.length >= 1 && s.events.length <= 20);
    for (const e of s.events) {
      assert.match(e.event_id, /^[0-9a-f-]{32,36}$/);
      for (const k of Object.keys(e)) assert.ok(!/user|session|cookie|agent|ip$/i.test(k), `field ${k}`);
    }
  }
}

async function look(page, name, width, scheme) {
  const wide = await page.evaluate(() => document.documentElement.scrollWidth);
  assert.ok(wide <= VIEWPORTS[width].width, `${name} scrolls sideways at ${width} (${wide} px)`);
  if (AXE) {
    await page.evaluate(AXE);  // over the DevTools protocol, which the page CSP does not apply to
    // Serious and critical findings fail, as in catalog_flow.js; lesser ones (all from before this change) are listed.
    const v = await page.evaluate(async () => (await window.axe.run(document, { resultTypes: ["violations"] })).violations
      .map((x) => ({ impact: x.impact, text: `${x.id}: ${x.nodes.map((n) => n.target.join(" ")).join(", ")}` })));
    assert.deepEqual(v.filter((x) => ["serious", "critical"].includes(x.impact)).map((x) => x.text), [], `axe (${name} ${width} ${scheme})`);
    for (const x of v) console.log(`axe ${x.impact} (${name} ${width} ${scheme}) ${x.text}`);
  }
  await page.screenshot({ path: `${out}/usage-${name}-${width}-${scheme}.png`, fullPage: true });
}

(async () => {
  const browser = await chromium.launch({
    // The collector's host is the test's collector, with the test's own certificate: a request sent as a page unloads
    // outlives the context, so the context's ignoreHTTPSErrors doesn't reach it. Only this test browser does this.
    args: [`--host-resolver-rules=MAP ${HOST} 127.0.0.1:${port}`, "--proxy-server=direct://", "--ignore-certificate-errors"],
  });
  try {
    // 1. Every site page, both widths and schemes: nothing leaves while it loads and is looked at; one page_view
    //    when it is hidden, from a direct visit.
    const PAGES = { landing: "/", catalog: "/catalog/", privacy: "/privacy/", terms: "/terms/", bot: "/bot/" };
    for (const [name, path] of Object.entries(PAGES)) {
      for (const width of ["390", "desktop"]) {
        for (const scheme of ["light", "dark"]) {
          const { context, page } = await open(browser, { viewport: VIEWPORTS[width], scheme });
          await page.goto(site + path, { waitUntil: "load" });
          if (name === "catalog") await settled(page);
          await page.waitForTimeout(300);
          await look(page, name, width, scheme);
          assert.equal(await count(), 0, `${name}: sent while loading`);
          await page.goto("about:blank");
          await until(async () => (await count()) === 1, `${name} page_view`);
          clean(await seen());
          const [e] = events(await seen());
          assert.deepEqual(
            { event: e.event, surface: e.surface, release: e.release, device: e.device, entry: e.entry, referrer: e.referrer, webdriver: e.webdriver },
            { event: "page_view", surface: name, release: build, device: width === "390" ? "phone" : "desktop", entry: true, referrer: undefined, webdriver: true },
          );
          await done(page, context);
        }
      }
    }

    // 2. The catalog: a search, a sort, then a click on the first result. Nothing leaves until the click; then the
    //    search (once: the sort alone is a filter_state, not another search) and the filter state, each counted once
    //    when it settled, and the click with a copy of both. The link carries the referral marker. The visit came
    //    from another site's page: only its host is kept.
    {
      const { context, page } = await open(browser);
      await page.goto(`${site}/catalog/`, { referer: "https://news.example.com/story/42?ref=x" });
      await settled(page);
      await page.fill("#q", "chicago");
      await page.waitForTimeout(2600);
      await page.selectOption("#sort", "asc");
      await page.waitForTimeout(2600);
      assert.equal(await count(), 0, "searching sent something before a click or hide");
      const link = page.locator("#rows a").first();
      const href = await link.getAttribute("href");
      assert.equal(href, "https://northside.coffee/?utm_source=registry.coffee&utm_medium=referral");
      await Promise.all([page.waitForURL(/northside\.coffee/), link.click()]);
      await until(async () => events(await seen()).length >= 4, "the catalog's events");
      clean(await seen());
      const got = events(await seen()).map(({ event_id, device, webdriver, release, surface, ...rest }) => rest);
      const chicago = { query: "chicago", cities: ["chicago"], states: [], origin: null, process: null, state: null, in_stock: true };
      assert.deepEqual(got, [
        { event: "page_view", entry: true, referrer: "news.example.com" },
        { event: "search", ...chicago, sort: "roaster", results_n: 1, zero_results: false },
        { event: "filter_state", origin: null, process: null, state: null, in_stock: true, sort: "asc", results_n: 1, zero_results: false },
        { event: "click_out", ...chicago, sort: "asc", results_n: 1, zero_results: false, roaster_id: "northside.coffee",
          coffee_id: "northside.coffee:windy-city-espresso", rank: 1 },
      ]);
      await done(page, context);
    }

    // 2b. Many searches on one page, past the 20-event batch: each event leaves exactly once, in batches of 20 or
    //     fewer. (A flush that queued while it flushed used to call itself and resend the same search.) The page's
    //     clock is run by hand: 60 searches, each settled, without 2 minutes of waiting.
    {
      const { context, page } = await open(browser);
      await page.goto(`${site}/catalog/`);
      await settled(page);
      await page.clock.install();
      for (let i = 1; i <= 60; i++) {
        await page.fill("#q", `zz${i}`);
        await page.clock.runFor(200);  // the typing pause
        await page.clock.runFor(2100);  // settled
        if (i % 15 === 0) { await page.selectOption("#sort", i % 30 ? "asc" : "roaster"); await page.clock.runFor(2100); }
      }
      await page.goto("about:blank");
      await until(async () => events(await seen()).length >= 65, "65 events");
      await sleep(500);
      const all = await seen();
      clean(all);
      const evs = events(all);
      assert.equal(evs.length, 65, `${evs.length} events arrived`);  // 1 page_view + 60 searches + 4 filter_states
      assert.equal(new Set(evs.map((e) => e.event_id)).size, 65);
      // Requests travel in parallel, so they may arrive in any order.
      const queries = evs.filter((e) => e.event === "search").map((e) => e.query).sort();
      assert.deepEqual(queries, Array.from({ length: 60 }, (_, i) => `zz${i + 1}`).sort());
      assert.equal(evs.filter((e) => e.event === "filter_state").length, 4);
      // Batches of at most 20 events and 4 KB (clean() and the collector check both), so a handful of requests.
      assert.ok(all.length <= 10, `${all.length} requests`);
      await done(page, context);
    }

    // 3. A campaign link: utm_* read (lower case), then taken out of the address, keeping the rest of it.
    {
      const { context, page } = await open(browser);
      await page.goto(`${site}/catalog/?utm_source=News&utm_medium=email&utm_campaign=Fall-2026&utm_content=<b>&b=2#browse`);
      await settled(page);
      assert.equal(await page.evaluate(() => location.search + location.hash), "?b=2#browse");
      await page.goto("about:blank");
      await until(async () => (await count()) === 1, "page_view");
      clean(await seen());
      const [e] = events(await seen());
      assert.deepEqual([e.entry, e.utm_source, e.utm_medium, e.utm_campaign, e.referrer], [true, "news", "email", "fall-2026", undefined]);
      await done(page, context);
    }

    // 4. The landing page's new arrivals: a click out with the coffee and roaster, no rank, no search.
    {
      const { context, page } = await open(browser, { viewport: VIEWPORTS.desktop });
      await page.goto(`${site}/`);
      const link = page.locator(".arrivals a").first();
      assert.equal(await link.getAttribute("href"), "https://northside.coffee/products/windy?variant=2&utm_source=registry.coffee&utm_medium=referral#top");
      await Promise.all([page.waitForURL(/northside\.coffee/), link.click()]);
      await until(async () => events(await seen()).length === 2, "landing events");
      clean(await seen());
      const [view, click] = events(await seen());
      assert.equal(view.event, "page_view");
      assert.deepEqual({ ...click, event_id: 0 }, { event_id: 0, surface: "landing", release: build, device: "desktop", webdriver: true,
        event: "click_out", roaster_id: "northside.coffee", coffee_id: "northside.coffee:windy-city-espresso", rank: null });
      await done(page, context);
    }

    // 5. Global Privacy Control, then Do Not Track: nothing is sent, and the address keeps its utm_*.
    for (const init of ["Object.defineProperty(Navigator.prototype, 'globalPrivacyControl', { get: () => true })",
                        "Object.defineProperty(Navigator.prototype, 'doNotTrack', { get: () => '1' })"]) {
      const { context, page } = await open(browser, { init });
      await page.goto(`${site}/catalog/?utm_source=news`);
      await settled(page);
      await page.fill("#q", "chicago");
      await page.waitForTimeout(2300);
      await Promise.all([page.waitForURL(/northside\.coffee/), page.locator("#rows a").first().click()]);
      await page.goto(`${site}/`);
      await page.goto("about:blank");
      await page.waitForTimeout(500);
      assert.equal(await count(), 0, `sent with ${init}`);
      await done(page, context);
    }

    // 6. A slow collector (10 s) and a failing one: pages load, the next page opens and a click out leaves at once.
    for (const collector of ["slow", "down"]) {
      const { context, page } = await open(browser, { collector });
      let t = Date.now();
      await page.goto(`${site}/`);
      await Promise.all([page.waitForURL(/\/catalog\/$/), page.click("text=Browse the catalog")]);
      await settled(page);
      assert.ok(Date.now() - t < 3000, `${collector}: landing to catalog took ${Date.now() - t} ms`);
      await until(async () => (await count()) === 1, `${collector}: the landing page_view`);
      await page.fill("#q", "chicago");
      await page.waitForTimeout(300);
      t = Date.now();
      await Promise.all([page.waitForURL(/northside\.coffee/), page.locator("#rows a").first().click()]);
      assert.ok(Date.now() - t < 1500, `${collector}: the click out took ${Date.now() - t} ms`);
      await until(async () => (await count()) === 2, `${collector}: the click`);
      clean(await seen());
      await done(page, context);
    }

    // 7. The app: a coffee's Shop link (a click out, with the referral marker), and the bag screen's catalog search
    //    (one search event per search, sent when the results arrive). Both widths and schemes; nothing on load.
    for (const width of ["390", "desktop"]) {
      for (const scheme of ["light", "dark"]) {
        const { context, page } = await open(browser, { viewport: VIEWPORTS[width], scheme });
        await page.goto(`${app}/coffee/bag1`);
        const shop = page.locator("a[data-out]");
        assert.equal(await shop.getAttribute("href"), "https://example.com/products/hambela?size=250&utm_source=registry.coffee&utm_medium=referral#buy");
        await look(page, "app-coffee", width, scheme);
        assert.equal(await count(), 0, "app: sent while loading");
        const [popup] = await Promise.all([context.waitForEvent("page"), shop.click()]);
        await until(async () => (await count()) === 1, "app click_out");
        await popup.close();
        await page.goto(`${app}/bag/bag1/edit`);
        await page.click("[data-search-open]");
        await page.fill("#catalog-query", "Hambela");
        await page.click("[data-search-submit]");
        await page.waitForSelector("[data-search-results] .pickrow");
        await until(async () => (await count()) === 2, "app search");
        await look(page, "app-search", width, scheme);
        clean(await seen());
        const got = events(await seen()).map(({ event_id, ...rest }) => rest);
        const device = width === "390" ? "phone" : "desktop";
        assert.deepEqual(got, [
          { release: null, device, webdriver: true, event: "click_out", surface: "app_coffee", roaster_id: "example.com", coffee_id: "example.com:1", rank: null },
          { release: null, device, webdriver: true, event: "search", surface: "app_search", query: "Hambela", results_n: 2, zero_results: false },
        ]);
        await done(page, context);
      }
    }

    // 8. The app with a slow, then a failing collector, then with Global Privacy Control: the search answers at once.
    for (const opts of [{ collector: "slow" }, { collector: "down" },
                        { init: "Object.defineProperty(Navigator.prototype, 'globalPrivacyControl', { get: () => true })" }]) {
      const { context, page } = await open(browser, opts);
      await page.goto(`${app}/bag/bag1/edit`);
      await page.click("[data-search-open]");
      await page.fill("#catalog-query", "Hambela");
      const t = Date.now();
      await page.click("[data-search-submit]");
      await page.waitForSelector("[data-search-results] .pickrow");
      assert.ok(Date.now() - t < 1500, `app search took ${Date.now() - t} ms`);
      await page.waitForTimeout(300);
      assert.equal(await count(), opts.init ? 0 : 1);
      clean(await seen());
      await done(page, context);
    }
    assert.deepEqual(errors, []);
  } catch (e) {
    console.error(e);
    console.error(errors.join("\n"));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
