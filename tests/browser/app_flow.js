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

// The notice appears empty and fills a moment later (so screen readers announce it): wait for its words.
async function noticeOf(page) {
  await page.waitForFunction(() => document.querySelector("[data-notice]")?.textContent);
  return page.textContent("[data-notice]");
}

async function newPage(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
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
    // Empty states: no bags, no shots, nobody invited yet.
    await first.goto(`${fresh}/coffees`);
    assert.match(await first.textContent("main"), /No bags yet/);
    await shoot(first, "coffees-empty");
    await first.goto(`${fresh}/history`);
    await shoot(first, "history-empty");
    await first.goto(`${fresh}/invite`);
    await shoot(first, "invite-empty");
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
    assert.equal(await page.textContent("[data-hint]"), "That username and password don’t match. Check them, or ask the owner for a reset code.");
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

    // A bag remembered on this phone that the server no longer selects (finished on another device, a cleared
    // cookie, a failed switch) never blocks the save: the server's bag wins.
    await page.evaluate(() => localStorage.setItem(`selected-bag:${document.querySelector('meta[name="user"]').content}`,
      JSON.stringify({ id: "finished-elsewhere", at: Date.now() })));
    // A slow save (the lake commit) shows at once: the button reads Saving… until the Saved page.
    let release;
    const slowSave = new Promise((ok) => { release = ok; });
    await page.route("**/api/shots", async (route) => { await slowSave; await route.continue(); });
    await page.getByRole("button", { name: "Save shot" }).click();
    const busy = page.locator("[data-save]");
    assert.equal(await busy.textContent(), "Saving…");
    assert.equal(await busy.isDisabled(), true);
    release();
    await page.waitForURL(/\/saved\/.+\?new=1$/);
    await page.unroute("**/api/shots");
    const saved = await page.textContent("main");
    assert.match(saved, /1:2\.03/);
    assert.match(saved, /Inside the window, one second faster than \w+day’s shot\./);
    assert.match(saved, /rated 5/);
    assert.match(saved, /Balanced/);
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
    assert.equal(await noticeOf(page), "Shot undone");
    // A bag can be changed after it's saved, from Today's "On the grinder": a roast date in the future isn't
    // saved, one with the wrong year asks again, and the change lands back on Today, saying so.
    const iso = (days) => new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);
    await page.getByRole("link", { name: "Edit bag" }).click();
    await page.waitForURL(`${base}/bag/bag1/edit?from=today`);
    await shoot(page, "bag-edit");
    // Searching the catalog for a coffee it doesn't have says so, rather than offering unrelated coffees.
    await page.getByRole("button", { name: "Search catalog" }).click();
    await page.fill("#catalog-query", "zzzzqq");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("[data-search-status]").textContent !== "Searching the catalog…");
    assert.equal(await page.textContent("[data-search-status]"), "No coffees found. Try another search.");
    assert.equal(await page.locator("[data-search-results] .pickrow").count(), 0);
    await page.fill("#catalog-query", "Hambela");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.waitForSelector("[data-search-results] .pickrow");
    assert.equal(await page.textContent("[data-search-status]"), "Choose the coffee on your bag.");
    await page.fill("#roast_date", iso(5));
    await page.getByRole("button", { name: "Save bag" }).click();
    assert.equal(await page.textContent("#bagedit [data-hint]"), "That roast date is in the future. Check the year.");
    await page.fill("#roast_date", "2016-10-15");
    await page.getByRole("button", { name: "Save bag" }).click();
    assert.match(await page.textContent("#bagedit [type=submit]"), /^Save with day \d{4}\?$/);
    assert.equal(new URL(page.url()).pathname, "/bag/bag1/edit");
    await page.fill("#roast_date", iso(-6));
    assert.equal(await page.textContent("#bagedit [type=submit]"), "Save bag");
    await page.getByRole("button", { name: "Save bag" }).click();
    await page.waitForURL(`${base}/`);
    assert.equal(await noticeOf(page), "Bag saved: Hambela Alaka");
    assert.match(await page.textContent("main"), /Roasted \w{3} \d+ · day 6 ·/);
    // A bag whose saved date is old keeps it without asking again when only the weight changes.
    await page.getByRole("link", { name: "Edit bag" }).click();
    await page.fill("#roast_date", iso(-200));
    await page.getByRole("button", { name: "Save bag" }).click();
    await page.getByRole("button", { name: /^Save with day \d+\?$/ }).click();
    await page.waitForURL(`${base}/`);
    await page.getByRole("link", { name: "Edit bag" }).click();
    await page.fill("#weight_g", "340");
    await page.getByRole("button", { name: "Save bag" }).click();
    await page.waitForURL(`${base}/`);
    assert.equal(await noticeOf(page), "Bag saved: Hambela Alaka");
    // Back from Today doesn't reopen Saved or the form: the save replaced them in history.
    await page.goBack();
    assert.notEqual(new URL(page.url()).pathname, "/log");
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
    assert.equal(await page.inputValue("#coffee"), ""); // nothing carried over from the photo before

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
    assert.equal(await noticeOf(page), "Correction saved");
    // Retract goes at once and offers Undo; Undo brings the shot back.
    await page.getByRole("button", { name: "Retract" }).click();
    await page.waitForSelector("[data-restore]:not([hidden])");
    // Quickly, in both themes: the strip leaves after 5 s.
    await page.screenshot({ path: `${out}/retracted-light.png` });
    await page.emulateMedia({ colorScheme: "dark" });
    await page.screenshot({ path: `${out}/retracted-dark.png` });
    await page.emulateMedia({ colorScheme: "light" });
    await axe(page, "retracted");
    await page.getByRole("button", { name: "Undo" }).click();
    await page.waitForSelector("[data-actions]:not([hidden])");
    assert.match(await page.textContent("main"), /puck was wet/);
    // Offline, Retract still acts at once (the retraction waits on the phone), and Undo takes it back (#18-3).
    await page.context().setOffline(true);
    await page.getByRole("button", { name: "Retract" }).click();
    await page.waitForSelector("[data-restore]:not([hidden])");
    assert.match(await page.textContent("[data-restore]"), /Retracted on this phone/);
    // The retraction is taken off the phone, and the shot's screen opens again (offline: the no-connection page),
    // saying so. Wait for that page, not just the words: the online Undo above left "Shot restored" showing, and a
    // goto racing the late navigation was interrupted.
    await Promise.all([page.waitForEvent("load"), page.getByRole("button", { name: "Undo" }).click()]);
    await page.waitForFunction(() => document.querySelector("[data-notice]")?.textContent === "Shot restored");
    await page.waitForFunction(() => new Promise((resolve) => {
      indexedDB.open("registry-coffee").onsuccess = (e) => {
        e.target.result.transaction("outbox").objectStore("outbox").count().onsuccess = (c) => resolve(c.target.result === 0);
      };
    }));
    await page.context().setOffline(false);
    // A correction made from a shot opened on Today goes back to it as opened, Back link and all.
    await page.goto(`${base}/shot/first?from=today`);
    await page.getByRole("link", { name: "Correct" }).click();
    await page.waitForURL(/\/log\?correct=first/);
    await page.getByRole("button", { name: "Save correction" }).click();
    await page.waitForURL(`${base}/shot/first?from=today`);
    assert.equal(await page.textContent("a.back"), "‹ Today");
    assert.equal(await noticeOf(page), "Correction saved");
    // Correcting with the time cleared falls back to that shot's own time (27 s), not the bag's latest (#18-1).
    await page.goto(`${base}/log?correct=dial3`);
    await page.fill("#time_s", "");
    await page.locator('[data-rating] [data-v="4"]').click();
    await page.getByRole("button", { name: "Save correction" }).click();
    assert.equal((await page.textContent("[data-save]")).trim(), "Save with 27 s?");
    await page.getByRole("button", { name: "More dose" }).click();
    assert.equal((await page.textContent("[data-save]")).trim(), "Save correction");

    await page.goto(`${base}/setup`);
    await shoot(page, "setup");
    // Left-handed: the steppers move to the left edge.
    await page.getByRole("button", { name: "Left", exact: true }).click();
    await page.goto(`${base}/log`);
    const [stepX, numX] = await page.evaluate(() => [
      document.querySelector(".field .steps").getBoundingClientRect().left,
      document.querySelector("#dose_g").getBoundingClientRect().left,
    ]);
    assert.ok(stepX < numX, "steppers sit left of the number");
    await shoot(page, "log-left");
    await page.goto(`${base}/setup`);
    await page.getByRole("button", { name: "Right", exact: true }).click();
    await page.goto(`${base}/setup`);
    await page.getByRole("link", { name: /DF64 V2/ }).click();
    await page.waitForURL(`${base}/setup/gear/g1`);
    await shoot(page, "gear");
    // Saving returns to Setup, saying so; Back from there doesn't land on the form again.
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await page.waitForURL(`${base}/setup`);
    assert.equal(await noticeOf(page), "DF64 V2 saved");
    await shoot(page, "gear-saved");
    // Equipment added from the gear picker on Log shot goes back to the picker.
    await page.goto(`${base}/log`);
    await page.getByRole("link", { name: /Grinder and basket/ }).click();
    await page.waitForURL(/\/log\/gear/);
    await page.getByRole("link", { name: "Add equipment" }).click();
    await page.getByRole("link", { name: "Basket", exact: true }).click();
    await page.waitForURL(/kind=basket/);
    assert.equal(await page.textContent("a.back"), "‹ Grinder and basket");
    await page.fill("#name", "VST 20 g");
    await page.fill("#basket_dose_g", "20");
    await page.getByRole("button", { name: "Only if I pick it" }).click(); // the shots below stay on the Pullman
    await page.getByRole("button", { name: "Add basket" }).click();
    await page.waitForURL(/\/log\/gear\?grinder=g1/);
    assert.equal(await noticeOf(page), "VST 20 g added");
    assert.match(await page.textContent("main"), /VST 20 g/);
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
    const offlineShot = async () => {
      await page.goto(`${base}/log`);
      await page.getByRole("button", { name: "Type" }).click();
      await page.fill("#time_s", "30");
      await page.locator('[data-rating] [data-v="4"]').click();
      await page.getByRole("button", { name: "Save shot" }).click();
      await page.waitForSelector("[data-offline-saved]:not([hidden])");
    };
    // Undo takes a shot that hasn't left the phone out of the outbox.
    await offlineShot();
    await page.getByRole("button", { name: "Undo" }).click();
    await page.waitForURL(`${base}/`);
    assert.equal(await outbox(), 0);
    await offlineShot();
    assert.equal(await page.isVisible("#shot"), false);
    assert.match(await page.textContent("[data-offline-saved] [data-sync]"), /1 waiting to sync/);
    assert.match(await page.textContent("[data-offline-saved]"), /Grind [\d.]+ · DF64 V2/);
    await shoot(page, "offline-saved");
    await page.goto(`${base}/bag/new`);
    await page.setInputFiles("#photo", bagPhoto);
    await page.waitForSelector('[data-step="queued"]:not([hidden])');
    assert.match(await page.textContent("main"), /Photo saved on this phone/);
    await shoot(page, "bag-offline");
    await page.getByRole("button", { name: "Type the coffee" }).last().click();
    assert.equal(await page.isVisible("[data-photo-waiting]"), true);
    await page.fill("#coffee", "the washed one");
    await page.getByRole("button", { name: "Use this coffee" }).last().click();
    // Saved on the phone: on to Today, saying so, instead of waiting on the form.
    await page.waitForURL(`${base}/`);
    assert.equal(await noticeOf(page), "Bag saved on this phone: the washed one. It’ll sync when you’re back online.");
    await shoot(page, "bag-offline-saved");
    assert.equal(await outbox(), 3);
    await page.context().setOffline(false);
    await page.waitForFunction(() => new Promise((resolve) => {
      indexedDB.open("registry-coffee").onsuccess = (e) => {
        e.target.result.transaction("outbox").objectStore("outbox").count().onsuccess = (c) => resolve(c.target.result === 0);
      };
    }), null, { timeout: 15000, polling: 250 });

    // Switching bags offline fails, and the kept Today and Log shot pages still open afterwards.
    await page.goto(`${base}/log`);
    await page.goto(`${base}/`);
    await page.goto(`${base}/bags/switch`);
    await page.context().setOffline(true);
    await page.locator("a.bagrow").first().click();
    await page.waitForURL(/\/bag\/[\w-]+\/use$/);
    assert.match(await page.textContent("h1"), /No connection/);
    await page.goto(`${base}/log`);
    assert.equal(await page.isVisible("#shot"), true);
    await page.goto(`${base}/`);
    assert.doesNotMatch(await page.textContent("h1"), /No connection/);
    await page.context().setOffline(false);

    // A date read off the label with the year wrong moves to the latest past one, and asks once before saving.
    await page.goto(`${base}/bag/new`);
    await page.setInputFiles("#photo", bagPhoto);
    await page.waitForSelector('[data-step="match"]:not([hidden])');
    await page.getByRole("button", { name: "Use this coffee" }).click();
    assert.match(await page.textContent("[data-use]"), /^Save with day \d+\?$/);
    assert.equal(new URL(page.url()).pathname, "/bag/new"); // not saved yet

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
    // A link mangled in the message: no error, the hash leaves the address bar, and the page says what to do.
    const mangled = await other.newPage();
    mangled.on("pageerror", (e) => errors.push(e.message));
    await mangled.goto(`${base}/join#K7QD-%E0`);
    assert.match(await mangled.textContent("[data-code-status]"), /didn’t come through whole/);
    assert.equal(new URL(mangled.url()).hash, "");
    await mangled.close();
    await guest.goto(`${base}/join#${code}`);
    await guest.waitForFunction(() => /Valid until/.test(document.querySelector("[data-code-status]").textContent));
    assert.equal(new URL(guest.url()).hash, ""); // the code left the address bar
    await shoot(guest, "join");
    // Another person signing in on a phone drops the pages kept for whoever used it before (#20).
    await guest.evaluate(() => caches.open("pages").then((c) => c.put("/history", new Response("someone else's"))));
    await guest.fill("#username", "maya");
    await guest.fill("#password", "lantern quarry velvet");
    await guest.getByRole("button", { name: "Create account" }).click();
    await guest.waitForURL(`${base}/`);
    assert.match(await guest.textContent("main"), /Nothing on the grinder yet/);
    assert.equal(await guest.evaluate(() => caches.has("pages").then(async (has) => has && (await (await caches.open("pages")).match("/history")) ? (await (await caches.open("pages")).match("/history")).text() : null)), null);
    await guest.goto(`${base}/reset`);
    await shoot(guest, "reset");
    await other.close();
    await page.goto(`${base}/invite`);
    assert.match(await page.textContent("main"), /maya/);
    await page.goto(`${base}/setup`);

    // A second tab changes the shared cookie while the first tab still has the owner's rendered page.
    const switcher = await page.context().newPage();
    const signInAs = async (username, password) => {
      await switcher.goto(`${base}/setup`);
      await switcher.getByRole("button", { name: "Sign out", exact: true }).click();
      await switcher.waitForURL(`${base}/signin`);
      await switcher.fill("#username", username);
      await switcher.fill("#password", password);
      await switcher.getByRole("button", { name: "Sign in" }).click();
      await switcher.waitForURL(`${base}/`);
    };
    await signInAs("maya", "lantern quarry velvet");
    const mismatch = await page.evaluate(async () => {
      const err = await send("bag:account-switch", "/api/bags", { bag_id: "account-switch", coffee_freetext: "Account switch test" }).catch((e) => e);
      return [err.constructor.name, Boolean(await store("readonly", (s) => s.get("bag:account-switch")))];
    });
    assert.deepEqual(mismatch, ["Held", true]); // kept on the phone, not saved as Maya
    await switcher.goto(`${base}/coffees`);
    assert.doesNotMatch(await switcher.textContent("main"), /Account switch test/);
    // An entry from before entries carried an account goes as the signed-in person, as before.
    await page.route("**/api/probe", (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as: r.request().headers()["x-expected-user"] }) }));
    const legacy = await page.evaluate(async () => {
      await store("readwrite", (s) => s.put({ key: "probe:legacy", url: "/api/probe", body: {}, at: Date.now() }));
      return [await deliver(await store("readonly", (s) => s.get("probe:legacy"))), await store("readonly", (s) => s.get("probe:legacy"))];
    });
    assert.deepEqual(legacy, [{ as: "owner" }, undefined]);
    await page.unroute("**/api/probe");
    await signInAs("jtouley", "kettle orbit marble");
    await page.evaluate(() => flush());
    assert.equal(await page.evaluate(() => store("readonly", (s) => s.get("bag:account-switch"))), undefined);
    await switcher.close();
    // A live label photo the server holds (403: another account, or the origin check) waits in the outbox, not dropped.
    await page.goto(`${base}/bag/new`);
    await page.route("**/api/photos/*", (r) => r.fulfill({ status: 403, contentType: "application/json", body: '{"detail":"Sign in as the person who made this save to sync it."}' }));
    await page.setInputFiles("#photo", bagPhoto);
    await page.waitForSelector('[data-step="queued"]:not([hidden])');
    const heldPhotos = await page.evaluate(async () => {
      const photos = (await store("readonly", (s) => s.getAll())).filter((e) => e.key.startsWith("photo:"));
      for (const e of photos) await store("readwrite", (s) => s.delete(e.key));
      return photos.map((e) => [e.user, e.blob instanceof Blob]);
    });
    assert.deepEqual(heldPhotos, [["owner", true]]);
    await page.unroute("**/api/photos/*");
    await page.goto(`${base}/setup`);

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
    // A write the server refuses (4xx) is remembered, so the offline saved view says so instead of "Synced" (#20-5).
    await page.route("**/api/shots", (r) => r.fulfill({ status: 422, contentType: "application/json", body: '{"detail":"Dose is too low."}' }));
    const no = await page.evaluate(async () => {
      await send("shot:probe", "/api/shots", {}).catch(() => {});
      await store("readwrite", (s) => s.delete("shot:probe")); // kept as "couldn't save"; not part of what follows
      return refused.get("shot:probe");
    });
    assert.equal(no, "Dose is too low.");
    await page.unroute("**/api/shots");

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

    // Times show in the phone's zone: the zone cookie arrives URL-encoded ("Asia%2FKolkata"), and read as is it
    // was UTC everywhere. Kolkata is UTC+5:30 all year.
    const clockIn = async (timezoneId) => {
      const ctx = await browser.newContext({ storageState: await page.context().storageState(), timezoneId });
      const p = await ctx.newPage();
      await p.goto(`${base}/setup`); // the first page sets the zone cookie
      await p.goto(`${base}/shot/first`);
      const [h, m] = (await p.textContent("h1")).match(/(\d{1,2}):(\d{2})$/).slice(1).map(Number);
      await ctx.close();
      return h * 60 + m;
    };
    assert.equal(((await clockIn("Asia/Kolkata")) - (await clockIn("UTC")) + 1440) % 1440, 330);

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
