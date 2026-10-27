// The app in Chromium at 390 px, against two running servers: one with no accounts yet (owner setup, first run)
// and one with a few shots (sign in, Today, Log shot -> Saved -> Undo). Every screen is checked with axe and shot
// in light and dark. Run by tests/test_app_browser.py.
// Usage: node app_flow.js <empty app url> <seeded app url> <screenshot dir>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [fresh, base, out, bagPhoto] = process.argv.slice(2);
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
    assert.equal(await page.locator('[data-taste] [aria-pressed="true"], [data-rating] [aria-pressed="true"]').count(), 0);
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
    // New bag from a photo: a clear match, an unclear one, an unreadable label.
    await page.goto(`${base}/bag/new`);
    await shoot(page, "bag-start");
    await page.setInputFiles("#photo", bagPhoto);
    await page.waitForSelector('[data-step="match"]:not([hidden])');
    assert.equal((await page.textContent('[data-f="title"]')).trim(), "Hambela Alaka");
    assert.match(await page.textContent('[data-f="roasted"]'), /read from the bag/);
    await shoot(page, "bag-match");
    await page.setInputFiles("#photo", bagPhoto);
    await page.waitForSelector('[data-step="pick"]:not([hidden])');
    assert.equal(await page.locator(".pickrow").count(), 2);
    await shoot(page, "bag-pick");
    await page.setInputFiles("#photo", bagPhoto);
    await page.waitForSelector('[data-step="failed"]:not([hidden])');
    await shoot(page, "bag-failed");
    await page.getByRole("button", { name: "Type the coffee" }).last().click();
    assert.equal(await page.isVisible("#bag"), true);

    // Coffees, one coffee, History, a shot, and correcting it.
    await page.goto(`${base}/coffees`);
    await shoot(page, "coffees");
    await page.goto(`${base}/coffee/bag1`);
    assert.match(await page.textContent("main"), /Best so far · rated 4/);
    await shoot(page, "coffee");
    await page.goto(`${base}/history`);
    await shoot(page, "history");
    await page.goto(`${base}/shot/first`);
    await shoot(page, "shot");
    await page.getByRole("link", { name: "Correct" }).click();
    await page.waitForURL(/\/log\?correct=first/);
    assert.equal(await page.inputValue("#time_s"), "29");
    await page.getByText("More").click();
    await page.fill("#notes", "puck was wet");
    await shoot(page, "correct");
    await page.getByRole("button", { name: "Save correction" }).click();
    await page.waitForURL(`${base}/shot/first`);
    assert.match(await page.textContent("main"), /puck was wet/);
    // Retract goes at once and offers Undo; Undo brings the shot back.
    await page.getByRole("button", { name: "Retract" }).click();
    await page.waitForSelector("[data-restore]:not([hidden])");
    await page.screenshot({ path: `${out}/retracted-light.png` }); // quickly: the strip leaves after 5 s
    await axe(page, "retracted");
    await page.getByRole("button", { name: "Undo" }).click();
    await page.waitForSelector("[data-actions]:not([hidden])");
    assert.match(await page.textContent("main"), /puck was wet/);

    await page.goto(`${base}/setup`);
    await shoot(page, "setup");
    await page.goto(`${base}/setup/gear/g1`);
    await shoot(page, "gear");
    await page.goto(`${base}/log`);
    await page.getByRole("link", { name: /Grinder and basket/ }).click();
    await page.waitForURL(/\/log\/gear/);
    await shoot(page, "pick-gear");

    // Offline: kept pages still open, a shot and a bag photo wait in the outbox, and both sync on reconnect.
    const outbox = () => page.evaluate(() => new Promise((resolve) => {
      indexedDB.open("registry-coffee").onsuccess = (e) => {
        e.target.result.transaction("outbox").objectStore("outbox").count().onsuccess = (c) => resolve(c.target.result);
      };
    }));
    await page.goto(`${base}/bag/new`); // kept for later, like the screens visited above
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.context().setOffline(true);
    await page.goto(`${base}/history`);
    assert.match(await page.textContent("main"), /History/);
    await page.goto(`${base}/log`);
    await page.getByRole("button", { name: "Type" }).click();
    await page.fill("#time_s", "30");
    await page.locator('[data-rating] [data-v="4"]').click();
    await page.getByRole("button", { name: "Save shot" }).click();
    await page.waitForSelector("[data-offline-saved]:not([hidden])");
    assert.equal(await page.isVisible("#shot"), false);
    assert.match(await page.textContent("[data-offline-saved] [data-sync]"), /1 waiting to sync/);
    await shoot(page, "offline-saved");
    await page.goto(`${base}/bag/new`);
    await page.setInputFiles("#photo", bagPhoto);
    await page.waitForSelector('[data-step="failed"]:not([hidden])');
    assert.match(await page.textContent('[data-f="failed-body"]'), /saved on this phone/);
    await shoot(page, "bag-offline");
    await page.getByRole("button", { name: "Type the coffee" }).last().click();
    await page.fill("#coffee", "the washed one");
    await page.getByRole("button", { name: "Use this coffee" }).last().click();
    await page.waitForFunction(() => /Saved on this phone/.test(document.querySelector("#bag [data-hint]").textContent));
    assert.equal(await outbox(), 3);
    await page.context().setOffline(false);
    await page.waitForFunction(() => new Promise((resolve) => {
      indexedDB.open("registry-coffee").onsuccess = (e) => {
        e.target.result.transaction("outbox").objectStore("outbox").count().onsuccess = (c) => resolve(c.target.result === 0);
      };
    }), null, { timeout: 15000, polling: 250 });

    // The owner invites someone; they join from the link on another phone.
    await page.goto(`${base}/invite`);
    await page.getByRole("button", { name: "Make an invite link" }).click();
    await page.waitForSelector("[data-codebox]:not([hidden])");
    const code = (await page.$$eval("[data-code] span", (els) => els.map((e) => e.textContent))).join("-");
    assert.equal(await page.textContent("[data-new-invite]"), "Copy link"); // the filled button now sends this code
    assert.match(code, /^[A-Z2-7]{4}(-[A-Z2-7]{4}){3}$/);
    await shoot(page, "invite");
    const other = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
    const guest = await other.newPage();
    guest.on("pageerror", (e) => errors.push(e.message));
    await guest.goto(`${base}/join#${code}`);
    await guest.waitForFunction(() => /Valid until/.test(document.querySelector("[data-code-status]").textContent));
    assert.equal(new URL(guest.url()).hash, ""); // the code left the address bar
    await shoot(guest, "join");
    await guest.fill("#username", "maya");
    await guest.fill("#password", "lantern quarry velvet");
    await guest.getByRole("button", { name: "Create account" }).click();
    await guest.waitForURL(`${base}/`);
    assert.match(await guest.textContent("main"), /Nothing on the grinder yet/);
    await guest.goto(`${base}/reset`);
    await shoot(guest, "reset");
    await other.close();
    await page.goto(`${base}/invite`);
    assert.match(await page.textContent("main"), /maya/);
    await page.goto(`${base}/setup`);

    // The shell is cached by the service worker.
    const cached = await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      const keys = await caches.keys();
      return keys.length ? (await (await caches.open(keys[0])).keys()).length : 0;
    });
    assert.ok(cached >= 5, `service worker cached ${cached} files`);

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
