// Email recovery in Chromium (docs/app/signup-ux-spec.md §4, §5, §12), against a server seeded by
// tests/test_email_browser.py: the owner (an unverified email), maya (a verified email) and sam (no email). Every
// state at 390 px and at a desktop width, light and dark, with axe on each.
// Usage: node email_flow.js <app url> <screenshot dir or ->
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [base, out] = process.argv.slice(2);
const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const OWNER = ["jtouley", "kettle orbit marble"];
const MAYA = ["maya", "lantern quarry velvet"];
const SAM = ["sam", "copper meadow tide"];
const SENT = /^If that account has a verified email, we’ve sent a link to it\. It works for 30 minutes\./;
const NO_EMAIL = "This account has no verified email, so we can’t send a link. Ask the person who invited you for a reset link.";
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

// Each state at 390 px and at a desktop width, light and dark, with axe on each.
async function snap(page, name) {
  const size = page.viewportSize();
  for (const [w, h] of [[390, 844], [1280, 900]]) {
    await page.setViewportSize({ width: w, height: h });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.waitForTimeout(150);
      await axe(page, `${name} ${w} ${scheme}`);
      if (out !== "-") {
        const file = `${out}/${name}-${w}-${scheme}.png`;
        await page.screenshot({ path: file, fullPage: true });
        shots.push(file);
      }
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize(size);
}

async function context(browser) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
  ctx.on("page", (p) => {
    p.on("pageerror", (e) => errors.push(`${p.url()}: ${e.message}`));
    p.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
  });
  return ctx;
}

const text = (page, sel) => page.evaluate((s) => document.querySelector(s)?.textContent ?? null, sel);
const waitText = (page, sel, re) => page.waitForFunction(([s, r]) => new RegExp(r).test(document.querySelector(s)?.textContent || ""), [sel, re.source]);

async function signIn(page, [name, pw]) {
  await page.goto(`${base}/signin`);
  await page.fill("#username", name);
  await page.fill("#password", pw);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => new URL(u).pathname !== "/signin");
}

// What the app "sent" (the FakeMailer, through the test's own route), read as the signed-in owner.
async function outbox(ownerPage) {
  return ownerPage.evaluate(async () => (await fetch("/__test/outbox")).json());
}
function linkIn(message, path) {
  const m = message.text.match(new RegExp(`https://app\\.registry\\.coffee/${path}#([A-Za-z0-9_\\-]+)`));
  assert.ok(m, message.text);
  return m[1];
}

(async () => {
  const browser = await chromium.launch();
  try {
    const ownerCtx = await context(browser);
    const owner = await ownerCtx.newPage();
    await signIn(owner, OWNER);

    // --- Sign-in: "Forgot your password?" goes to /forgot, and nothing else is said there ---------------------
    const anonCtx = await context(browser);
    const anon = await anonCtx.newPage();
    await anon.goto(`${base}/signin`);
    const forgotLink = anon.getByRole("link", { name: "Forgot your password?", exact: true });
    assert.equal(await forgotLink.getAttribute("href"), "/forgot");
    assert.doesNotMatch(await text(anon, "main"), /Ask the person who invited you/);
    await snap(anon, "signin-forgot-link");
    await forgotLink.click();
    await anon.waitForURL(`${base}/forgot`);
    assert.equal(await anon.evaluate(() => document.title), "Forgot password · registry.coffee");
    assert.doesNotMatch(await text(anon, "main"), /No email on your account/, "only the inline error says that");
    await snap(anon, "forgot");

    // --- /forgot: empty, then a malformed email, then an account with no email -------------------------------
    const send = () => anon.getByRole("button", { name: "Send reset link" }).click();
    await send();
    await waitText(anon, "#account-error", /^Enter your username or email\.$/);
    assert.equal(await anon.getAttribute("#account", "aria-invalid"), "true");
    assert.equal(await anon.evaluate(() => document.activeElement.id), "account");
    await snap(anon, "forgot-empty");
    await anon.fill("#account", "sam");
    await send();
    await waitText(anon, "#account-error", new RegExp(`^${NO_EMAIL.replace(/[.?]/g, "\\$&")}$`));
    assert.equal(await anon.getAttribute("#account", "aria-invalid"), "true");
    assert.equal(await anon.evaluate(() => document.activeElement.id), "account", "focus on the field with the error");
    assert.equal(await anon.inputValue("#account"), "sam", "what was typed stays");
    await snap(anon, "forgot-no-email");

    // --- /forgot by username: sent. The reply is the same for no such account ---------------------------------
    await anon.fill("#account", "maya");
    const posts = [];
    anon.on("request", (r) => { if (r.method() === "POST" && r.url().endsWith("/api/forgot")) posts.push(r); });
    await anon.getByRole("button", { name: "Send reset link" }).dblclick();
    await waitText(anon, "[data-sent]", SENT);
    assert.equal(posts.length, 1, "a double tap sends one request");
    assert.equal(await anon.getAttribute("#account", "aria-invalid"), null, "the error went with the fix");
    assert.equal(await text(anon, "#account-error"), "");
    assert.equal(await anon.evaluate(() => document.querySelector("[data-notice]").hidden), true, "not the top notice");
    assert.equal(await anon.evaluate(() => {
      const [h, sent, form] = ["h1", "[data-sent]", "#forgot"].map((q) => document.querySelector(q).getBoundingClientRect().top);
      return h < sent && sent < form;
    }), true, "under the heading, above the form");
    await snap(anon, "forgot-sent");
    await anon.fill("#account", "nobody-here");
    await anon.getByRole("button", { name: "Send reset link" }).click();
    await waitText(anon, "[data-sent]", SENT);
    assert.equal(await text(anon, "#account-error"), "", "an unknown name gets the same reply, no error");
    let mails = await outbox(owner);
    const resetMail = mails.filter((m) => m.to === "maya@example.com" && /Reset/.test(m.subject)).at(-1);
    assert.ok(resetMail, "maya got the reset email");
    assert.equal(mails.filter((m) => /Reset/.test(m.subject)).length, 1, "nothing for sam or nobody");

    // --- The emailed link: /reset fills the code, the new password ends on sign-in with the username ---------
    const code = linkIn(resetMail, "reset");
    await anon.goto(`${base}/reset#${code}`);
    assert.equal(await anon.inputValue("#code"), code);
    assert.equal(new URL(anon.url()).hash, "", "the code leaves the address bar");
    assert.match(await text(anon, "main"), /Use the link from your email, or ask the person who invited you for a reset link\./);
    await snap(anon, "reset-from-email");
    await anon.fill("#password", "a fresh pour over");
    await anon.getByRole("button", { name: "Set password" }).click();
    await anon.waitForURL(`${base}/signin`);
    await waitText(anon, "[data-notice]", /^Password changed\. Sign in with the new one\.$/);
    assert.equal(await anon.inputValue("#username"), "maya");
    assert.equal(await anon.evaluate(() => document.activeElement.id), "password");
    await snap(anon, "signin-after-reset");
    await anon.fill("#password", "a fresh pour over");
    await anon.getByRole("button", { name: "Sign in" }).click();
    await anon.waitForURL(`${base}/`);
    mails = await outbox(owner);
    assert.ok(mails.some((m) => m.to === "maya@example.com" && /was changed/.test(m.subject)), "the changed notice");
    await anonCtx.close();

    // --- Joining: the email field, a bad address on blur, the once-only nudge, then with an email -----------
    await owner.goto(`${base}/invite`);
    await owner.getByRole("button", { name: "Make an invite link" }).click();
    await owner.waitForSelector("[data-codebox]:not([hidden])");
    const invite = (await owner.$$eval("[data-code] span", (els) => els.map((e) => e.textContent))).join("-");
    const joinCtx = await context(browser);
    const joiner = await joinCtx.newPage();
    await joiner.goto(`${base}/join#${invite}`);
    assert.equal(await joiner.isChecked("#marketing"), true, "marketing is ticked to start with");
    assert.equal(await joiner.isChecked("#agree"), false);
    await joiner.fill("#username", "ana");
    await joiner.fill("#email", "ana@example");
    await joiner.fill("#password", "quiet morning grind");
    await waitText(joiner, "#email-error", /^That doesn’t look like an email address\. Check for typos\.$/);
    assert.equal(await joiner.getAttribute("#email", "aria-invalid"), "true");
    await snap(joiner, "join-email-invalid");
    await joiner.fill("#email", "");
    await joiner.check("#agree");
    await joiner.getByRole("button", { name: "Create account" }).click();
    await joiner.waitForSelector("[data-email-nudge]:not([hidden])");
    assert.equal(await joiner.evaluate(() => document.activeElement.id), "nudge-text");
    assert.equal(new URL(joiner.url()).pathname, "/join", "the nudge holds the form");
    await snap(joiner, "join-nudge");
    await joiner.getByRole("button", { name: "Add email" }).click();
    assert.equal(await joiner.evaluate(() => document.activeElement.id), "email");
    await joiner.fill("#email", "Ana@Example.COM");
    await joiner.getByRole("button", { name: "Create account" }).click();
    await joiner.waitForURL(`${base}/`);
    await waitText(joiner, "[data-notice]", /^Welcome, ana\. Check your inbox for a link to confirm your email\.$/);
    assert.equal(await joiner.locator("[data-email-prompt]").count(), 0, "no prompt: she added one");
    await snap(joiner, "joined-with-email");
    mails = await outbox(owner);
    const verifyMail = mails.filter((m) => m.to === "Ana@example.com" && /Confirm/.test(m.subject)).at(-1);
    assert.ok(verifyMail, "the confirm email, to the address with its domain lower-cased");

    // --- Setup: not verified yet, Send again; then the verify link; then a used link ------------------------
    await joiner.goto(`${base}/setup`);
    await waitText(joiner, "[data-email-status]", /^Ana@example\.com · not verified yet$/);
    await snap(joiner, "setup-email-unverified");
    await joiner.getByRole("button", { name: /^Send again/ }).click();
    await waitText(joiner, "[data-notice]", /^Sent\. The new link works for 24 hours; the old one no longer does\.$/);
    mails = await outbox(owner);
    const fresh = mails.filter((m) => m.to === "Ana@example.com").at(-1);
    await joiner.goto(`${base}/verify#${linkIn(verifyMail, "verify")}`);
    await waitText(joiner, "[data-verify-title]", /^Link expired$/);
    assert.match(await text(joiner, "[data-verify-status]"), /^That link has expired or was already used\. Send a new one from Setup\.$/);
    assert.equal(await joiner.getAttribute("[data-verify-status] a", "href"), "/setup#email");
    await snap(joiner, "verify-expired");
    await joiner.goto("about:blank"); // a link opens the page afresh; only the # changing wouldn't reload it
    await joiner.goto(`${base}/verify#${linkIn(fresh, "verify")}`);
    await waitText(joiner, "[data-verify-title]", /^Email verified$/);
    assert.equal(await text(joiner, "[data-verify-status]"), "You can now reset your password by email.");
    assert.equal(await joiner.evaluate(() => document.activeElement.matches("[data-verify-title]")), true);
    await snap(joiner, "verify-done");
    await joiner.goto(`${base}/setup`);
    await waitText(joiner, "[data-email-status]", /^Ana@example\.com · verified$/);
    // The marketing box saves as it's ticked.
    await joiner.uncheck("#marketing");
    await waitText(joiner, "[data-marketing-status]", /^Saved: you won’t get them\.$/);
    await snap(joiner, "setup-email-verified");
    // Roaster insights: on to start with; turning it off saves, says so, and stays off after a reload.
    assert.equal(await joiner.isChecked("#insights"), true);
    assert.equal(await text(joiner, "label[for=insights]"), "Include my shots and bags in roaster insights");
    assert.equal(await text(joiner, "[data-insights-status]"), "", "no hint that describes sales as happening");
    await joiner.uncheck("#insights");
    await waitText(joiner, "[data-insights-status]", /^Saved: later reports will leave out your shots and bags\.$/);
    await joiner.locator("#insights").scrollIntoViewIfNeeded();
    await snap(joiner, "setup-insights-off");
    await joiner.reload();
    assert.equal(await joiner.isChecked("#insights"), false, "kept");
    // Change: it needs the current password (review M3); a wrong one is said on its field, then it goes through.
    await joiner.locator("summary", { hasText: "Change email" }).click();
    await joiner.fill("#new-email", "ana@new.example");
    await joiner.getByRole("button", { name: "Send link" }).click();
    await waitText(joiner, "#email-current-error", /^Enter your current password\.$/);
    await joiner.fill("#email-current", "not my password");
    await joiner.getByRole("button", { name: "Send link" }).click();
    await waitText(joiner, "#email-current-error", /^Your current password isn’t right\.$/);
    assert.equal(await joiner.inputValue("#new-email"), "ana@new.example", "what was typed stays");
    await snap(joiner, "setup-change-email-password");
    await joiner.fill("#email-current", "quiet morning grind");
    await joiner.getByRole("button", { name: "Send link" }).click();
    await joiner.waitForURL(`${base}/setup`);
    await waitText(joiner, "[data-notice]", /^Check your inbox for a link to confirm ana@new\.example\.$/);
    // Remove: the current password too, in its own small form.
    await joiner.locator("summary", { hasText: "Remove email" }).click();
    await joiner.fill("#remove-current", "quiet morning grind");
    await snap(joiner, "setup-remove-email");
    await joiner.locator("#email-remove-form").getByRole("button", { name: "Remove email" }).click();
    await joiner.waitForURL(`${base}/setup`);
    await waitText(joiner, "[data-notice]", /^Email removed\. The owner can still make you a reset link\.$/);
    await waitText(joiner, "[data-email-status]", /^No email$/);
    await joiner.locator("summary", { hasText: "Add email" }).click();
    await snap(joiner, "setup-no-email");
    await joinCtx.close();

    // --- Today, for a member with no email: the prompt, then Not now for 90 days ------------------------------
    const samCtx = await context(browser);
    const sam = await samCtx.newPage();
    await signIn(sam, SAM);
    await sam.waitForSelector("[data-email-prompt]");
    await snap(sam, "today-email-prompt");
    await sam.getByRole("button", { name: "Not now" }).click();
    assert.equal(await sam.locator("[data-email-prompt]").count(), 0);
    const cookie = (await samCtx.cookies()).find((c) => c.name === "nudge-email");
    assert.ok(cookie && cookie.expires - Date.now() / 1000 > 89 * 86400, "Not now lasts 90 days");
    await sam.reload();
    assert.equal(await sam.locator("[data-email-prompt]").count(), 0, "and stays gone");
    await sam.goto(`${base}/setup#email`);
    assert.equal(await sam.evaluate(() => document.querySelector("[data-email-edit]").open), true, "Add email opens the form");
    await samCtx.close();

    // --- The owner's People screen: each member's email and its state, and Remove ---------------------------
    await owner.goto(`${base}/invite`);
    assert.match(await text(owner, "main"), /maya@example\.com · verified/);
    assert.match(await text(owner, "main"), /sam[\s\S]*No email/);
    await snap(owner, "people-emails");

    await ownerCtx.close();
    assert.deepEqual(errors, []);
    assert.deepEqual(problems, [], `axe found serious or critical issues:\n${problems.join("\n")}`);
    console.log(JSON.stringify({ screenshots: shots.length }));
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  if (errors.length) console.error("Page errors:", errors);
  process.exit(1);
});
