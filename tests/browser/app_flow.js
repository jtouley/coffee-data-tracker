// The app in Chromium at 390 px, against two running servers: one with no accounts yet (owner setup, first run)
// and one with a few shots (sign in, Today, Log shot -> Saved -> Undo). Every screen is checked with axe and shot
// in light and dark. Run by tests/test_app_browser.py.
// Usage: node app_flow.js <empty app url> <seeded app url> <screenshot dir>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [fresh, base, out] = process.argv.slice(2);
const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const problems = [];

async function axe(page, name) {
  // Evaluated over the DevTools protocol, which the page's CSP doesn't apply to.
  await page.evaluate(AXE);
  const found = await page.evaluate(async () => {
    const r = await window.axe.run(document, { resultTypes: ["violations"] });
    return r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(" ")) }));
  });
  for (const v of found) {
    if (v.impact === "serious" || v.impact === "critical") problems.push(`${name}: ${v.id} (${v.impact}) ${v.nodes.join(", ")}`);
  }
}

async function shoot(page, name) {
  await page.evaluate(() => { document.activeElement?.blur(); scrollTo(0, 0); });
  await page.waitForTimeout(300); // let the 120 ms selection fades finish
  for (const scheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(250); // colour transitions finish before axe reads the colours
    await axe(page, `${name} (${scheme})`);
    // The whole screen at 390 px wide, so nothing sits under the sticky Save button.
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    await page.setViewportSize({ width: 390, height: Math.max(844, height) });
    await page.screenshot({ path: `${out}/${name}-${scheme}.png` });
    await page.setViewportSize({ width: 390, height: 844 });
  }
  await page.emulateMedia({ colorScheme: "light" });
}

async function newPage(browser, errors) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
  return page;
}

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  try {
    // A new app: the first account is the owner's, made with the setup code.
    const first = await newPage(browser, errors);
    await first.goto(`${fresh}/`);
    assert.equal(new URL(first.url()).pathname, "/signin");
    await shoot(first, "owner-setup");
    await first.getByRole("button", { name: "Create account" }).click();
    assert.equal(await first.textContent("[data-hint]"), "Fill in every field.");
    await first.fill("#setup_code", "SETUP-CODE-1234");
    await first.fill("#username", "jtouley");
    await first.fill("#password", "kettle orbit marble");
    await first.getByRole("button", { name: "Show" }).click();
    assert.equal(await first.getAttribute("#password", "type"), "text");
    await first.getByRole("button", { name: "Create account" }).click();
    await first.waitForURL(`${fresh}/`);
    assert.match(await first.textContent("main"), /Nothing on the grinder yet/);
    await shoot(first, "first-run");
    await first.goto(`${fresh}/setup`);
    await shoot(first, "setup-empty");
    await first.goto(`${fresh}/bag/new`);
    await shoot(first, "new-bag");
    await first.goto(`${fresh}/offline`);
    await shoot(first, "offline");
    await first.goto(`${fresh}/saved/missing`);
    await shoot(first, "not-found");
    await first.close();

    // An app with shots: sign in, then Today -> Log shot -> Saved -> Undo.
    const page = await newPage(browser, errors);
    await page.goto(`${base}/`);
    assert.equal(new URL(page.url()).pathname, "/signin");
    await shoot(page, "sign-in");
    await page.fill("#username", "jtouley");
    await page.fill("#password", "not the password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForFunction(() => document.querySelector("[data-hint]").textContent !== "");
    assert.equal(await page.textContent("[data-hint]"), "That username and password don’t match. Check both and try again.");
    await shoot(page, "sign-in-error");
    await page.fill("#password", "kettle orbit marble");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(`${base}/`);
    await shoot(page, "today");
    await page.getByRole("link", { name: /Pull again/ }).click();
    await page.waitForURL(`${base}/log`);

    // Prefilled from the last shot; time, taste and rating start empty.
    assert.equal(await page.inputValue("#dose_g"), "18.0");
    assert.equal(await page.inputValue("#yield_g"), "36.4");
    assert.equal(await page.inputValue("#grinder_setting"), "11");
    assert.equal(await page.locator('[aria-pressed="true"]').count(), 0);
    assert.match(await page.textContent("[data-untimed]"), /Not timed · last 29 s/);
    assert.doesNotMatch(await page.textContent("main"), /finer|coarser|Try \d/);

    await page.getByRole("button", { name: "More yield" }).click();
    await page.getByRole("button", { name: "More yield" }).click();
    await page.getByRole("button", { name: "Lower grind number" }).click();
    assert.equal(await page.inputValue("#grinder_setting"), "10.5");
    assert.equal(await page.textContent("[data-ratio]"), "1:2.03");

    // Save without a time asks once before using the last one; typing a time skips that.
    await page.getByRole("button", { name: "Save shot" }).click();
    assert.equal(await page.textContent(".hint"), "Pick a rating to save.");
    await page.getByRole("button", { name: /Same as last/ }).click();
    assert.equal(await page.getAttribute('[data-taste] [data-v="balanced"]', "aria-pressed"), "true");
    await page.getByRole("button", { name: "Type" }).click();
    await page.fill("#time_s", "28");
    await page.locator('[data-rating] [data-v="5"]').click();
    await shoot(page, "log-shot");

    await page.getByRole("button", { name: "Save shot" }).click();
    await page.waitForURL(/\/saved\/.+\?new=1$/);
    const saved = await page.textContent("main");
    assert.match(saved, /1:2\.03/);
    assert.match(saved, /Inside the window, one second faster than \w+day’s shot\./);
    assert.match(saved, /Balanced · 5/);
    await shoot(page, "saved");

    // Nothing left waiting in the outbox once the server has acknowledged the shot.
    const waiting = await page.evaluate(() => new Promise((resolve) => {
      indexedDB.open("registry-coffee").onsuccess = (e) => {
        e.target.result.transaction("outbox").objectStore("outbox").count().onsuccess = (c) => resolve(c.target.result);
      };
    }));
    assert.equal(waiting, 0);
    process.stdout.write(`${page.url().split("/saved/")[1].split("?")[0]}\n`);

    await page.getByRole("button", { name: "Undo" }).click();
    await page.waitForURL(`${base}/`);
    assert.match(await page.textContent("main"), /Last shot.*\n?.*29/s);
    await page.goto(`${base}/setup`);
    await shoot(page, "setup");

    // The shell is cached by the service worker.
    const cached = await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      return (await (await caches.open("static")).keys()).length; // hashed files, kept across versions
    });
    assert.ok(cached >= 5, `service worker cached ${cached} files`);

    // A 403 (the origin check, which a proxy can trip by mistake) keeps the write on the phone (#15-2).
    await page.route("**/api/equipment", (r) => r.fulfill({ status: 403, contentType: "application/json", body: '{"detail":"no"}' }));
    const held = await page.evaluate(async () => {
      const err = await send("probe:1", "/api/equipment", {}).catch((e) => e);
      const n = await new Promise((resolve) => {
        indexedDB.open("registry-coffee").onsuccess = (e) => {
          e.target.result.transaction("outbox").objectStore("outbox").count().onsuccess = (c) => resolve(c.target.result);
        };
      });
      await store("readwrite", (s) => s.delete("probe:1"));
      return [err.constructor.name, n];
    });
    assert.deepEqual(held, ["Held", 1]);
    await page.unroute("**/api/equipment");

    // The outbox only lets go of a write the server acknowledged (#13): every status, what's left of the entry.
    const table = {};
    for (const status of [200, 400, 401, 403, 404, 408, 409, 422, 429, 500, 503]) {
      await page.route("**/api/probe", (r) => r.fulfill({ status, contentType: "application/json", headers: { "Retry-After": "120" }, body: status === 200 ? "{}" : '{"detail":"no"}' }));
      table[status] = await page.evaluate(async (st) => {
        await send(`probe:${st}`, "/api/probe", {}).catch(() => {});
        const e = await store("readonly", (s) => s.get(`probe:${st}`));
        await store("readwrite", (s) => s.delete(`probe:${st}`));
        return e ? (e.failed ? "shown as couldn't save" : "kept to retry") : "removed";
      }, status);
      await page.unroute("**/api/probe");
    }
    assert.deepEqual(table, {
      200: "removed", 400: "shown as couldn't save", 401: "kept to retry", 403: "kept to retry",
      404: "shown as couldn't save", 408: "kept to retry", 409: "shown as couldn't save",
      422: "shown as couldn't save", 429: "kept to retry", 500: "kept to retry", 503: "kept to retry",
    });
    // A refused write is listed on screen until dismissed.
    await page.route("**/api/probe", (r) => r.fulfill({ status: 422, contentType: "application/json", body: '{"detail":"Dose is too low."}' }));
    await page.evaluate(() => send("shot:probe", "/api/probe", {}).catch(() => {}));
    await page.waitForSelector("[data-failed]:not([hidden])");
    assert.match(await page.textContent("[data-failed]"), /A shot from .*: Dose is too low\./);
    await page.locator("[data-failed]").getByRole("button", { name: "Dismiss" }).click();
    await page.waitForSelector("[data-failed]", { state: "hidden" });
    await page.unroute("**/api/probe");

    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await page.waitForURL(`${base}/signin`);
    assert.deepEqual(errors, []);
    assert.deepEqual(problems, [], `axe found serious or critical issues:\n${problems.join("\n")}`);
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
