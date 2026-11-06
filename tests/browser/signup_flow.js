// Sign-up and invites in Chromium (docs/app/signup-ux-spec.md §12), against three running servers seeded by
// tests/signup_seed.py: a normal one, one whose users-dataset commits take 6 s, and one whose Terms are newer than
// what everyone accepted. Run by tests/test_signup_browser.py.
// Usage: node signup_flow.js <app url> <slow app url> <bumped-terms app url> <screenshot dir or -> <passwords.json>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [base, slow, bumped, out, fixtures] = process.argv.slice(2);
const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const OWNER = ["jtouley", "kettle orbit marble"];
const MEMBER = ["maya", "lantern quarry velvet"];
const INSTAGRAM = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 390.0.0.27.105 (iPhone15,2; iOS 18_5; en_US; en; scale=3.00; 1179x2556; 739125451)";
const FACEBOOK = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/480.0.0.40.104;FBBV/700000000;FBDV/iPhone15,2;FBMD/iPhone;FBSN/iOS;FBSV/18.5;FBSS/3;FBID/phone;FBLC/en_US;FBOP/5]";
const SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
const CHROME = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36";
// A 1Password Random password with symbols, and one with every symbol it uses.
const ONEPASSWORD = JSON.parse(fs.readFileSync(fixtures, "utf8")).find((p) => p.kind === "random" && p.symbols && p.password.length === 20).password;
const problems = [];
const errors = [];
const shots = [];

async function axe(page, name) {
  await page.evaluate(AXE); // over the DevTools protocol, which the page's CSP doesn't apply to
  const found = await page.evaluate(async () => {
    const r = await window.axe.run(document, { resultTypes: ["violations"] });
    return r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(" ")) }));
  });
  for (const v of found) if (v.impact === "serious" || v.impact === "critical") problems.push(`${name}: ${v.id} (${v.impact}) ${v.nodes.join(", ")}`);
}

// Each state at 375 px and at a desktop width, light and dark, with axe on each.
async function snap(page, name, { full = true } = {}) {
  const size = page.viewportSize();
  for (const [w, h] of [[375, 812], [1280, 900]]) {
    await page.setViewportSize({ width: w, height: h });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.waitForTimeout(200);
      await axe(page, `${name} ${w} ${scheme}`);
      if (out !== "-") {
        const file = `${out}/${name}-${w}-${scheme}.png`;
        await page.screenshot({ path: file, fullPage: full });
        shots.push(file);
      }
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize(size);
}

async function context(browser, { width = 390, height = 844, userAgent, init } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, userAgent });
  if (init) await ctx.addInitScript(init);
  ctx.on("page", (p) => {
    p.on("pageerror", (e) => errors.push(`${p.url()}: ${e.message}`));
    if (process.env.FLOW_DEBUG) p.on("response", (r) => console.error(r.status(), r.request().method(), r.url()));
    p.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
  });
  return ctx;
}

const text = (page, sel) => page.evaluate((s) => document.querySelector(s)?.textContent ?? null, sel);
const waitText = (page, sel, re) => page.waitForFunction(([s, r]) => new RegExp(r).test(document.querySelector(s)?.textContent || ""), [sel, re.source]);

async function signIn(page, url, [name, pw]) {
  await page.goto(`${url}/signin`);
  await page.fill("#username", name);
  await page.fill("#password", pw);
  await page.getByRole("button", { name: "Sign in" }).click();
}

// The owner makes a join link, with or without a note, and reads the code off the screen.
async function makeInvite(page, url, note = "") {
  await page.goto(`${url}/invite`);
  if (note) await page.fill("#for_label", note);
  await page.getByRole("button", { name: "Make an invite link" }).click();
  await page.waitForSelector("[data-codebox]:not([hidden])");
  return (await page.$$eval("[data-code] span", (els) => els.map((e) => e.textContent))).join("-");
}

async function fillJoin(page, username, password) {
  await page.fill("#username", username);
  await page.fill("#password", password);
  await page.check("#agree");
}

function counting(page, path) {
  const sent = [];
  page.on("request", (r) => { if (r.method() === "POST" && new URL(r.url()).pathname === path) sent.push(r); });
  return sent;
}

(async () => {
  const browser = await chromium.launch();
  try {
    // --- The owner: an invite with a "For" note, and a double tap makes one link ---------------------------------
    const ownerCtx = await context(browser);
    const owner = await ownerCtx.newPage();
    await signIn(owner, base, OWNER);
    await owner.waitForURL(`${base}/`);
    await owner.goto(`${base}/invite`);
    assert.match(await text(owner, "main"), /Sam from the café\s*open/);
    assert.match(await text(owner, "main"), /Maya, from the roastery\s*used/);
    assert.match(await text(owner, "main"), /Used by maya on/);
    assert.match(await text(owner, "main"), /Old link for Ana\s*revoked/);
    assert.match(await text(owner, "main"), /Lee\s*expired/);
    await owner.fill("#for_label", "Jo at the market");
    assert.equal(await text(owner, "[data-new-note] [data-count]"), "16 of 60");
    await snap(owner, "invite-list");
    const invitePosts = counting(owner, "/api/invites");
    await owner.getByRole("button", { name: "Make an invite link" }).dblclick();
    await owner.waitForSelector("[data-codebox]:not([hidden])");
    assert.equal(invitePosts.length, 1, "a double tap made one link");
    assert.equal(await text(owner, "[data-code-label]"), "Invite for Jo at the market");
    assert.equal(await owner.evaluate(() => document.activeElement.matches("[data-code-label]")), true);
    await waitText(owner, "main [data-status]", /Invite link ready/);
    const code = (await owner.$$eval("[data-code] span", (els) => els.map((e) => e.textContent))).join("-");
    await snap(owner, "invite-ready");
    // A note over 60 characters is refused in place.
    await owner.goto(`${base}/invite`);
    await owner.fill("#for_label", "x".repeat(61));
    await owner.getByRole("button", { name: "Make an invite link" }).click();
    assert.equal(await text(owner, "[data-new-note] .ferr"), "Keep the note to 60 characters.");

    // --- Joining: idle, reload keeps the code, "@", 1Password, terms unticked then ticked, a double tap ----------
    const guestCtx = await context(browser, { width: 375, height: 812 });
    const guest = await guestCtx.newPage();
    await guest.goto(`${base}/join#${code}`);
    await waitText(guest, "#code-h", /Valid until/);
    assert.equal(new URL(guest.url()).hash, "");
    await guest.reload();
    assert.equal(await guest.inputValue("#code"), code, "the code survives a reload");
    await waitText(guest, "#code-h", /Valid until/);
    await snap(guest, "join-idle");
    await guest.fill("#username", "Maya.Lee@gmail.com");
    await guest.waitForSelector("[data-suggest]:not([hidden])");
    assert.equal(await text(guest, "#username-error"), "A username isn’t an email address. Try maya.lee.");
    assert.equal(await text(guest, "[data-suggest] button"), "Use maya.lee");
    assert.equal(await guest.getAttribute("#username", "aria-invalid"), null, "no red while typing");
    await snap(guest, "join-at-sign");
    await guest.getByRole("button", { name: "Use maya.lee" }).click();
    assert.equal(await guest.inputValue("#username"), "maya.lee");
    assert.equal(await text(guest, "#username-error"), "");
    await guest.fill("#password", ONEPASSWORD.slice(0, 6)); // typing: length not met yet, the rest wait
    assert.deepEqual(await guest.$$eval("#password-rules li", (els) => els.map((e) => e.dataset.state)), ["", "", "", ""]);
    await guest.fill("#password", ONEPASSWORD); // pasted from 1Password
    assert.deepEqual(await guest.$$eval("#password-rules li", (els) => els.map((e) => e.dataset.state)), ["met", "met", "met", "met"]);
    await snap(guest, "join-typing");
    // Invalid: a bad username left on blur, and the terms unticked on submit.
    await guest.fill("#username", "m");
    await guest.focus("#password");
    assert.equal(await guest.getAttribute("#username", "aria-invalid"), "true");
    assert.equal(await text(guest, "#username-error"), "Use 3 to 32 characters.");
    assert.equal(await guest.getAttribute("#password", "aria-invalid"), null, "only the field that failed");
    await guest.fill("#username", "maya.lee");
    await guest.getByRole("button", { name: "Create account" }).click();
    assert.equal(await guest.evaluate(() => document.activeElement.id), "agree");
    assert.equal(await text(guest, "#agree-error"), "Tick the box to agree to the Terms and Privacy Policy before creating your account.");
    assert.equal(await guest.getAttribute("#agree", "aria-invalid"), "true");
    assert.equal(await guest.getAttribute("#password", "aria-invalid"), null);
    assert.equal(guestCtx.pages().length, 1, "nothing opened a tab");
    await snap(guest, "join-invalid");
    // The links open a new tab only when clicked; ticking the box opens nothing.
    assert.deepEqual(await guest.$$eval(".agree a", (as) => as.map((a) => [a.target, a.rel])), [["_blank", "noopener"], ["_blank", "noopener"]]);
    await guest.click(".agree label", { position: { x: 4, y: 8 } });
    assert.equal(await guest.isChecked("#agree"), true);
    assert.equal(guestCtx.pages().length, 1);
    const joins = counting(guest, "/api/join");
    // Back from the desktop-width screenshots: settle the scroll before the two taps land on the button.
    await guest.evaluate(() => document.querySelector("#join [type=submit]").scrollIntoView({ block: "center" }));
    await guest.getByRole("button", { name: "Create account" }).dblclick();
    await guest.waitForURL(`${base}/`);
    assert.equal(joins.length, 1, "a double tap sent one join");
    await waitText(guest, "[data-notice]", /Welcome, maya\.lee\./);
    await snap(guest, "join-success");
    assert.equal(await guest.evaluate(() => sessionStorage.getItem("join-code")), null);

    // --- Already used: says so, and "sign in" goes to sign-in ------------------------------------------------
    const usedCtx = await context(browser, { width: 375, height: 812 });
    const used = await usedCtx.newPage();
    await used.goto(`${base}/join#${code}`);
    await waitText(used, "#code-h", /already been used/);
    assert.equal(await used.getAttribute("#code", "aria-invalid"), "true");
    await snap(used, "join-already-used");
    await used.locator("#code-h").getByRole("link", { name: "sign in" }).click();
    await used.waitForURL(`${base}/signin`);
    assert.match(await text(used, "main"), /Forgot your password\? Ask the person who invited you for a reset link/);
    await snap(used, "sign-in");
    await usedCtx.close();

    // --- A lost reply: the join reaches the server, the reply never comes back, the page sends it again ----------
    const lostCode = await makeInvite(owner, base, "Lost reply test");
    const lostCtx = await context(browser, { init: () => { delete window.caches; } }); // and a WebView without caches
    const lost = await lostCtx.newPage();
    let first = true;
    await lost.route("**/api/join", async (route) => {
      if (first) {
        first = false;
        await route.fetch(); // the server makes the account...
        return route.abort("connectionreset"); // ...and the phone never hears
      }
      return route.continue();
    });
    const lostSent = counting(lost, "/api/join");
    await lost.goto(`${base}/join#${lostCode}`);
    await fillJoin(lost, "lost.reply", "orbit kettle marble");
    await lost.getByRole("button", { name: "Create account" }).click();
    await lost.waitForURL(`${base}/`, { timeout: 20000 });
    assert.equal(lostSent.length, 2, "the same join, sent again once");
    assert.equal(await lost.evaluate(() => "caches" in window), false);
    await lostCtx.close();

    // --- In-app browsers: the banner, rendered by the server, and Copy link ----------------------------------
    for (const [ua, name] of [[INSTAGRAM, "Instagram"], [FACEBOOK, "Facebook"]]) {
      const ctx = await context(browser, { width: 375, height: 812, userAgent: ua });
      const p = await ctx.newPage();
      await p.goto(`${base}/join#${lostCode}`);
      assert.match(await text(p, "[data-in-app]"), new RegExp(`Opening from ${name}\\?`));
      if (name === "Instagram") {
        await snap(p, "join-in-app");
        await p.getByRole("button", { name: "Copy link" }).click(); // no clipboard permission here
        await p.waitForSelector("[data-copy-fallback]:not([hidden])");
        assert.equal(await p.inputValue("[data-copy-fallback] input"), `${base}/join#${lostCode}`);
        await snap(p, "join-in-app-copy");
      }
      await ctx.close();
    }
    for (const ua of [SAFARI, CHROME]) {
      const ctx = await context(browser, { userAgent: ua });
      const p = await ctx.newPage();
      await p.goto(`${base}/join`);
      assert.equal(await p.$("[data-in-app]"), null);
      await ctx.close();
    }

    // --- The invite screen: edit and clear a note, with their busy state -------------------------------------
    await owner.goto(`${base}/invite`);
    const samId = await owner.locator("[data-invite]", { hasText: "Sam from the café" }).getAttribute("data-invite");
    const row = owner.locator(`[data-invite="${samId}"]`);
    await row.getByRole("button", { name: "Edit note" }).click();
    await row.locator("input").fill("Sam, café on 5th");
    await snap(owner, "invite-edit-note");
    await row.getByRole("button", { name: "Save" }).click();
    await waitText(owner, "main [data-status]", /Note saved/);
    assert.match(await row.textContent(), /Sam, café on 5th/);
    await row.getByRole("button", { name: "Edit note" }).click();
    await row.locator("input").fill("");
    await row.getByRole("button", { name: "Save" }).click();
    await waitText(owner, "main [data-status]", /Note cleared/);
    await owner.reload();
    assert.equal(await owner.locator("[data-invite]", { hasText: "Sam" }).count(), 0, "cleared after a reload too");

    // --- A reset link: confirm, busy, copy; the member uses it -----------------------------------------------
    await ownerCtx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: base });
    await owner.goto(`${base}/invite`);
    const reset = owner.locator('[data-reset][data-name="maya"]');
    assert.equal(await reset.textContent(), "Make a reset link for maya");
    await reset.click();
    assert.equal(await reset.textContent(), "Tap again: maya’s old reset link stops working");
    await owner.getByRole("button", { name: "Tap again: maya’s old reset link stops working" }).click();
    await owner.waitForSelector("[data-codebox]:not([hidden])");
    assert.equal(await text(owner, "[data-code-label]"), "Reset link for maya");
    assert.match(await text(owner, "[data-code-note]"), /^Send it to maya\. It works once, until .+, and signs them out everywhere\.$/);
    await owner.getByRole("button", { name: "Copy link" }).click();
    await waitText(owner, "[data-new-invite]", /^Copied$/);
    const resetLink = await owner.evaluate(() => navigator.clipboard.readText());
    assert.match(resetLink, new RegExp(`^${base}/reset#[A-Z2-7]{4}(-[A-Z2-7]{4}){3}$`));
    await snap(owner, "invite-reset-link");
    const memberCtx = await context(browser, { width: 375, height: 812 });
    const member = await memberCtx.newPage();
    await member.goto(resetLink);
    assert.equal(await member.inputValue("#code"), resetLink.split("#")[1]);
    await member.fill("#password", "new lantern quarry");
    await snap(member, "reset");
    await member.getByRole("button", { name: "Set password" }).click();
    await member.waitForURL(`${base}/`);
    await memberCtx.close();

    // --- A slow server: busy, then the 4 s line; five taps, one request -------------------------------------
    const slowOwnerCtx = await context(browser);
    const slowOwner = await slowOwnerCtx.newPage();
    await signIn(slowOwner, slow, OWNER);
    await slowOwner.waitForURL(`${slow}/`, { timeout: 20000 });
    await slowOwner.goto(`${slow}/invite`);
    await slowOwner.getByRole("button", { name: "Make an invite link" }).click();
    assert.equal(await text(slowOwner, "[data-new-invite]"), "Making a link…");
    await waitText(slowOwner, "main [data-status]", /Still saving/);
    await slowOwner.waitForSelector("[data-codebox]:not([hidden])", { timeout: 20000 });
    const slowCode = (await slowOwner.$$eval("[data-code] span", (els) => els.map((e) => e.textContent))).join("-");
    const slowCtx = await context(browser, { width: 375, height: 812 });
    const slowPage = await slowCtx.newPage();
    const slowSent = counting(slowPage, "/api/join");
    await slowPage.goto(`${slow}/join#${slowCode}`);
    await fillJoin(slowPage, "patient", "slow kettle marble");
    const button = slowPage.getByRole("button", { name: "Create account" });
    await button.click();
    for (let i = 0; i < 4; i++) await slowPage.locator("#join [type=submit]").click({ force: true }); // aria-disabled: Playwright would wait
    assert.equal(await slowPage.getAttribute("#join [type=submit]", "aria-disabled"), "true");
    assert.equal(await text(slowPage, "#join [type=submit]"), "Creating account…");
    assert.equal(await slowPage.getAttribute("#username", "readonly"), "");
    await snap(slowPage, "join-busy");
    await waitText(slowPage, "#join [data-status]", /Still working\. The app may be waking up/);
    await snap(slowPage, "join-slow");
    await slowPage.waitForURL(`${slow}/`, { timeout: 30000 });
    assert.equal(slowSent.length, 1, "five taps sent one join");
    await slowCtx.close();
    await slowOwnerCtx.close();

    // --- New terms: sign-in lands on /agree; writes wait; Accept needs the tick; Sign out signs out ------------
    const agreeCtx = await context(browser, { width: 375, height: 812 });
    const agree = await agreeCtx.newPage();
    await signIn(agree, bumped, MEMBER);
    await agree.waitForURL(`${bumped}/agree`);
    await agree.goto(`${bumped}/setup`);
    assert.equal(new URL(agree.url()).pathname, "/agree", "every screen waits for the new terms");
    // A write waits (403, which the phone's outbox keeps); without the gate this one would be a 400.
    const held = await agree.evaluate(async () => (await fetch("/api/password", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" }, body: "{}" })).status);
    assert.equal(held, 403);
    await snap(agree, "agree");
    await agree.getByRole("button", { name: "Accept" }).click();
    assert.equal(await agree.evaluate(() => document.activeElement.id), "agree");
    assert.match(await text(agree, "#agree-error"), /Tick the box/);
    await snap(agree, "agree-invalid");
    await agree.check("#agree");
    await agree.getByRole("button", { name: "Accept" }).click();
    await agree.waitForURL(`${bumped}/`);
    await agree.goto(`${bumped}/agree`);
    assert.equal(new URL(agree.url()).pathname, "/", "accepted once is enough");
    await agreeCtx.close();
    const outCtx = await context(browser);
    const leaving = await outCtx.newPage();
    await signIn(leaving, bumped, OWNER);
    await leaving.waitForURL(`${bumped}/agree`);
    await leaving.getByRole("button", { name: "Sign out" }).click();
    await leaving.waitForURL(`${bumped}/signin`);
    await leaving.goto(`${bumped}/`);
    assert.equal(new URL(leaving.url()).pathname, "/signin");
    await outCtx.close();

    // --- Narrow phones: the error line is in view above the keyboard's dock, at every width -----------------
    for (const width of [320, 360, 390, 430]) {
      const ctx = await context(browser, { width, height: 640 });
      const p = await ctx.newPage();
      await p.goto(`${base}/join#${lostCode}`);
      await p.fill("#username", "a@b");
      await p.fill("#password", "short");
      await p.check("#agree");
      await p.getByRole("button", { name: "Create account" }).click();
      assert.equal(await p.evaluate(() => document.activeElement.id), "username");
      const box = await p.locator("#username-error").boundingBox();
      assert.ok(box && box.y >= 0 && box.y + box.height <= 640, `error line in view at ${width}`);
      const wide = await p.evaluate(() => document.documentElement.scrollWidth);
      assert.ok(wide <= width, `no sideways scroll at ${width}`);
      await ctx.close();
    }

    await ownerCtx.close();
    assert.deepEqual(errors, []);
    assert.deepEqual(problems, [], `axe found serious or critical issues:\n${problems.join("\n")}`);
    console.log(JSON.stringify({ screenshots: shots.length }));
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
