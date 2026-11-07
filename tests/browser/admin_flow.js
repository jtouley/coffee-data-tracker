// The admin in a browser (tests/test_admin_browser.py starts the servers and writes the config):
// passkeys through WebAuthn virtual authenticators, every page and state, at 390 px and 1,280 px, light and dark,
// with axe on each. Every check runs even if an earlier one failed; the matrix is printed and saved as matrix.json.
// Usage: node admin_flow.js <config.json>
"use strict";
const fs = require("fs");
const assert = require("assert/strict");
const { chromium } = require("playwright");

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = cfg.out;
const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const matrix = [];
const problems = [];

async function check(name, fn) {
  try {
    await fn();
    matrix.push({ check: name, result: "pass" });
  } catch (e) {
    matrix.push({ check: name, result: "FAIL", detail: String(e && e.message ? e.message : e).slice(0, 600) });
  }
}

async function axe(page, name) {
  await page.evaluate(AXE); // over the DevTools protocol, which the page's CSP doesn't apply to
  const found = await page.evaluate(async () => {
    const r = await window.axe.run(document, { resultTypes: ["violations"] });
    return r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(" ")) }));
  });
  const bad = found.filter((v) => v.impact === "serious" || v.impact === "critical");
  for (const v of bad) problems.push(`${name}: ${v.id} (${v.impact}) ${v.nodes.slice(0, 3).join(", ")}`);
  return bad;
}

// Every screen at 390 px and 1,280 px, light and dark: axe, no sideways scroll at 390, and a full-page screenshot.
async function shoot(page, name) {
  await page.evaluate(() => { document.activeElement?.blur(); scrollTo(0, 0); });
  const bad = [];
  for (const [w, h, tag] of [[390, 844, "390"], [1280, 900, "desktop"]]) {
    await page.setViewportSize({ width: w, height: h });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.waitForTimeout(150);
      bad.push(...(await axe(page, `${name} ${tag} ${scheme}`)));
      if (w === 390) {
        const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        if (over > 0) problems.push(`${name} 390: the page scrolls sideways by ${over}px`);
      }
      await page.screenshot({ path: `${out}/${name}-${tag}-${scheme}.png`, fullPage: true });
    }
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  assert.deepEqual(bad.map((v) => v.id), [], `axe: ${name}`);
}

async function authenticator(cdp) {
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
               isUserVerified: true, automaticPresenceSimulation: true },
  });
  return authenticatorId;
}

async function settled(page) {
  await page.waitForSelector("section[data-part][aria-busy='false']", { timeout: 40000 });
}

async function answer(page) {
  return (await page.textContent("[data-answer]")).replace(/\s+/g, " ").trim();
}

async function api(page, path, body, headers = { "X-Requested-With": "fetch" }) {
  return page.evaluate(async ([path, body, headers]) => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
    return r.status;
  }, [path, body, headers]);
}

(async () => {
  const browser = await chromium.launch({ args: ["--host-resolver-rules=MAP localhost 127.0.0.1"] });
  const base = cfg.main;
  const [w1, w2, w3] = cfg.weeks;
  const [b1, b2] = cfg.builds;
  const errors = [];
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  const cdp = await ctx.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  let codes = [];
  let first = null; // the first passkey's authenticator and its credential, to sign in with it again later
  let second = null;

  try {
    await check("signed out: a page sends you to enrollment while no passkey exists", async () => {
      await page.goto(`${base}/week`);
      await page.waitForURL(/\/enroll$/);
      const r = await page.request.get(`${base}/part/week`);
      assert.equal(r.status(), 401);
      const unknown = await page.request.get(`${base}/no-such-page`, { maxRedirects: 0 });
      assert.equal(unknown.status(), 303); // an unknown path is signed-in like any other
      await shoot(page, "enroll");
    });

    await check("enrollment: a wrong token is refused", async () => {
      await page.fill("#token", "not-the-enrollment-token-but-long-enough-0123");
      await page.click("button[type=submit]");
      await page.waitForFunction(() => document.querySelector("[data-hint]").textContent.includes("isn’t the enrollment token") || document.querySelector("[data-hint]").textContent.includes("isn't the enrollment token"));
      await shoot(page, "enroll-wrong-token");
    });

    await check("enrollment: the first passkey, and ten recovery codes shown once", async () => {
      first = { id: await authenticator(cdp) };
      await page.fill("#token", cfg.token);
      await page.click("button[type=submit]");
      await page.waitForSelector("[data-codes]:not([hidden])");
      codes = await page.$$eval("[data-code-list] li", (li) => li.map((x) => x.textContent));
      assert.equal(codes.length, 10);
      const creds = (await cdp.send("WebAuthn.getCredentials", { authenticatorId: first.id })).credentials;
      assert.equal(creds.length, 1);
      first.credential = creds[0];
      await shoot(page, "enroll-codes");
    });

    await check("enrollment is closed after the first passkey", async () => {
      const p2 = await ctx.newPage();
      await p2.goto(`${base}/enroll`);
      assert.match(await p2.textContent("main"), /Enrollment is closed/);
      assert.equal(await api(p2, "/api/auth/enroll/begin", { token: cfg.token }), 403);
      await p2.close();
    });

    await check("the second passkey, from the enrollment session", async () => {
      await page.click("text=I’ve saved them");
      await page.waitForURL(/\/account$/);
      await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId: first.id });
      second = { id: await authenticator(cdp) };
      await page.click("form[data-flow=add] button[type=submit]");
      await page.waitForFunction(() => /Passkeys \(2\)/.test(document.querySelector("caption").textContent), null, { timeout: 15000 });
      await shoot(page, "account");
    });

    await check("sign out, then sign in with the second passkey", async () => {
      await page.click("button[data-signout='/api/auth/signout']");
      await page.waitForURL(/\/signin$/);
      await shoot(page, "signin");
      await page.click("form[data-flow=signin] button[type=submit]");
      await page.waitForURL(/\/week$/);
      await settled(page);
    });

    await check("sign in with the first passkey", async () => {
      await page.click("text=Account");
      await page.click("button[data-signout='/api/auth/signout']");
      await page.waitForURL(/\/signin$/);
      await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId: second.id });
      first.id = await authenticator(cdp);
      await cdp.send("WebAuthn.addCredential", { authenticatorId: first.id, credential: first.credential });
      await page.goto(`${base}/signin?next=/release`);
      await page.click("form[data-flow=signin] button[type=submit]");
      await page.waitForURL(/\/release$/);
      await settled(page);
    });

    await check("a POST without the same-origin markers is refused (CSRF)", async () => {
      assert.equal(await api(page, "/api/auth/signout", {}, {}), 403);
    });

    await check("Week: the newest week has fetches but no jobs or release yet (empty + stale)", async () => {
      await page.goto(`${base}/week`);
      await settled(page);
      assert.match(await page.textContent(".stale"), new RegExp(`Serving week ${w1}; week ${w3} has data`));
      assert.equal(await page.getAttribute("[data-state]", "data-state"), "empty");
      await shoot(page, "week-empty-stale");
    });

    await check("Week: a week that landed", async () => {
      await page.selectOption("#week", w2);
      await page.click(".picker button");
      await page.waitForURL(new RegExp(`week=${w2}`));
      await settled(page);
      const a = await answer(page);
      assert.match(a, /5 of 5 connectors landed/);
      assert.match(a, /build failed/);
      assert.ok(await page.isVisible("text=crawl-retry"));
      await shoot(page, "week");
    });

    await check("Week's failures: counts by HTTP status, challenge, partial and quarantined, the trend, a Test crawl link", async () => {
      await page.goto(`${base}/week?week=${w2}`);
      await settled(page);
      const a = (await page.textContent("#failures ~ .answer")).replace(/\s+/g, " ");
      assert.match(a, /6 stores need a look/);
      assert.match(a, /siteground 1 · cloudflare 1 · partial 1 · quarantined 1/);
      assert.match(await page.textContent("main"), /Across weeks/);
      const store = page.locator("a.ev", { hasText: "sg-challenge.example" });
      assert.equal(await store.getAttribute("href"), "https://sg-challenge.example/");
      assert.ok((await page.getAttribute("a:text('Test crawl') >> nth=0", "href")).startsWith("/jobs?action=crawl-one"));
      await page.locator("#failures").scrollIntoViewIfNeeded();
      await shoot(page, "week-failures");
    });

    await check("Release: the served build's gate passes; the failed build shows its failing test first", async () => {
      await page.goto(`${base}/release?build=${b1}`);
      await settled(page);
      assert.match(await page.textContent("#gate ~ .answer"), new RegExp(`Build ${b1} passed`));
      assert.match(await page.textContent("main"), /Actions are off/); // no database here: said once, reads work
      await shoot(page, "release");
      await page.goto(`${base}/release?build=${b2}`);
      await settled(page);
      assert.match(await page.textContent("#gate ~ .answer"), new RegExp(`Build ${b2} failed: 1 failing`));
      assert.match(await page.textContent("tbody tr.bad"), /run_ok_rate/);
      await shoot(page, "release-failed");
    });

    await check("Release: changes by table, change and reason, and the rows behind a count", async () => {
      await page.goto(`${base}/release?build=${b2}`);
      await settled(page);
      assert.match(await page.textContent("#changes ~ .answer"), /rows change/);
      await page.click("#changes ~ .frame table td.n a >> nth=0");
      await settled(page);
      assert.match(await page.textContent("main"), /show all/);
      await shoot(page, "release-changes-filtered");
      await page.goto(`${base}/release?build=${b1}`);
      await settled(page);
      assert.match(await page.textContent("main"), /No changes|rows change|Not recorded/);
    });

    await check("Catalog: search, facet filters with counts, removable chips, zero results", async () => {
      await page.goto(`${base}/catalog`);
      await settled(page);
      const total = Number((await answer(page)).match(/of ([\d,]+) products/)[1].replace(/,/g, ""));
      assert.ok(total > 20);
      await shoot(page, "catalog");
      await page.fill("#q", "onyx");
      await page.click(".searchrow button");
      await settled(page);
      const found = Number((await answer(page)).match(/^([\d,]+) of/)[1].replace(/,/g, ""));
      assert.ok(found > 0 && found < total);
      if (!(await page.isVisible("select[name=coffee]"))) await page.click("[data-filters] > summary");
      const option = await page.$eval("select[name=coffee] option:nth-child(2)", (o) => o.value);
      await page.selectOption("select[name=coffee]", option); // submits on change
      await page.waitForURL(/coffee=/);
      await settled(page);
      assert.equal(await page.locator(".chips .chip").count(), 2);
      await shoot(page, "catalog-filtered");
      await page.click(".chips .chip >> nth=0"); // remove the search
      await page.waitForURL((u) => !u.searchParams.get("q"));
      await settled(page);
      assert.equal(await page.locator(".chips .chip").count(), 1);
      await page.fill("#q", "no such coffee anywhere");
      await page.click(".searchrow button");
      await settled(page);
      assert.match(await page.textContent("main"), /No products match/);
      await shoot(page, "catalog-zero");
    });

    await check("an expired session sends you to sign in, saying so", async () => {
      const c2 = await browser.newContext();
      await c2.addCookies([{ name: "admin_session", value: cfg.cookies.expired, url: base }]);
      const p2 = await c2.newPage();
      await p2.goto(`${base}/week`);
      await p2.waitForURL(/\/signin\?.*ended=1/);
      assert.match(await p2.textContent("main"), /Your session ended/);
      await shoot(p2, "session-ended");
      await c2.close();
    });

    await check("a cookie signed with another secret is refused", async () => {
      const c2 = await browser.newContext();
      await c2.addCookies([{ name: "admin_session", value: cfg.cookies.foreign, url: base }]);
      const p2 = await c2.newPage();
      await p2.goto(`${base}/release`);
      await p2.waitForURL(/\/signin/);
      const r = await p2.request.get(`${base}/part/release`);
      assert.equal(r.status(), 401);
      await c2.close();
    });

    await check("recovery: a code registers a new passkey, retires the lost ones, signs out every session; it works once", async () => {
      await page.goto(`${base}/account`);
      await page.click("button[data-signout='/api/auth/signout-all']");
      await page.waitForURL(/\/signin$/);
      await cdp.send("WebAuthn.removeVirtualAuthenticator", { authenticatorId: first.id });
      await authenticator(cdp);
      await page.goto(`${base}/recover`);
      await page.fill("#code", codes[0].toLowerCase().replace(/-/g, ""));
      await shoot(page, "recover");
      await page.click("button[type=submit]");
      await page.waitForURL(/\/week$/);
      await page.goto(`${base}/account`);
      assert.match(await page.textContent("caption"), /Passkeys \(1\)/); // the lost ones are retired
      assert.match(await page.textContent("main"), /9 of 10 unused/);
      await page.click("button[data-signout='/api/auth/signout']");
      await page.waitForURL(/\/signin$/);
      await page.goto(`${base}/recover`);
      await page.fill("#code", codes[0]);
      await page.click("button[type=submit]");
      await page.waitForFunction(() => /used/.test(document.querySelector("[data-hint]").textContent));
    });

    await check("broken session secret: the service is switched off, fails closed", async () => {
      const c2 = await browser.newContext();
      const p2 = await c2.newPage();
      const r = await p2.goto(`${cfg.off}/week`);
      assert.equal(r.status(), 503);
      assert.match(await p2.textContent("main"), /switched off/);
      assert.doesNotMatch(await p2.textContent("main"), /too-short/);
      assert.equal((await p2.request.get(`${cfg.off}/health`)).status(), 503);
      assert.equal((await p2.request.get(`${cfg.off}/part/week`)).status(), 503);
      assert.equal((await p2.request.post(`${cfg.off}/api/auth/signin/begin`, { data: {} })).status(), 503);
      await shoot(p2, "switched-off");
      await c2.close();
    });

    for (const name of ["week", "jobs", "release", "catalog", "log"]) {
      await check(`no data: ${name} on an empty lake`, async () => {
        const c2 = await browser.newContext();
        await c2.addCookies([{ name: "admin_session", value: cfg.cookies.empty, url: cfg.empty }]);
        const p2 = await c2.newPage();
        p2.on("pageerror", (e) => errors.push(String(e)));
        await p2.goto(`${cfg.empty}/${name}`);
        await settled(p2);
        assert.equal(await p2.getAttribute("[data-state]", "data-state"), "empty");
        await shoot(p2, `empty-${name}`);
        await c2.close();
      });
    }

    await check("slow lake: the cup and the slow note, then the data", async () => {
      const c2 = await browser.newContext({ viewport: { width: 390, height: 844 } });
      await c2.addCookies([{ name: "admin_session", value: cfg.cookies.slow, url: cfg.slow }]);
      const p2 = await c2.newPage();
      await p2.goto(`${cfg.slow}/week`);
      await p2.waitForSelector("[data-loading] .cup");
      await p2.waitForSelector("[data-slow-note]:not([hidden])", { timeout: 5000 });
      assert.equal(await p2.getAttribute("section[data-part]", "aria-busy"), "true");
      for (const scheme of ["light", "dark"]) {
        await p2.emulateMedia({ colorScheme: scheme });
        await axe(p2, `loading-slow 390 ${scheme}`);
        await p2.screenshot({ path: `${out}/loading-slow-390-${scheme}.png`, fullPage: true });
      }
      await p2.setViewportSize({ width: 1280, height: 900 });
      await p2.emulateMedia({ colorScheme: "light" });
      await p2.screenshot({ path: `${out}/loading-slow-desktop-light.png`, fullPage: true });
      await p2.emulateMedia({ colorScheme: "dark" });
      await p2.screenshot({ path: `${out}/loading-slow-desktop-dark.png`, fullPage: true });
      await settled(p2);
      assert.ok(await p2.isVisible("[data-state]"));
      await c2.close();
    });

    await check("lake error: the error state names the file and offers Retry", async () => {
      const c2 = await browser.newContext();
      await c2.addCookies([{ name: "admin_session", value: cfg.cookies.broken, url: cfg.broken }]);
      const p2 = await c2.newPage();
      await p2.goto(`${cfg.broken}/week`);
      await settled(p2);
      assert.match(await p2.textContent("[data-state=error]"), /Couldn’t read/);
      assert.match(await p2.textContent("[data-state=error]"), /HTTP 503/);
      await shoot(p2, "error");
      await p2.click("[data-retry]");
      await settled(p2);
      assert.ok(await p2.isVisible("[data-state=error]"));
      await c2.close();
    });

    await check("no script errors on any page", async () => assert.deepEqual(errors, []));
    await check("axe: no serious or critical issues, and no sideways scroll at 390 px", async () =>
      assert.deepEqual(problems, [], problems.join("\n")));
  } finally {
    await browser.close();
    fs.writeFileSync(`${out}/matrix.json`, JSON.stringify(matrix, null, 1));
    for (const m of matrix) console.log(`${m.result.padEnd(4)}  ${m.check}${m.detail ? `\n      ${m.detail}` : ""}`);
  }
  process.exit(matrix.some((m) => m.result !== "pass") ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
