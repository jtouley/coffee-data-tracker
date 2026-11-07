// The admin's writes in a browser (tests/test_admin_browser.py, test_admin_actions_in_a_browser, starts the servers,
// a fake Cloud Run with its control port, and a local Postgres): every job state, Catalog decisions with Undo and a
// group, a draft, Publish, Roll back and Discard, and the refusals and failures, at 390 px and 1,280 px, light and
// dark, with axe on each. Every check runs even if an earlier one failed; the matrix is printed and saved.
// Usage: node admin_actions.js <config.json>
"use strict";
const fs = require("fs");
const assert = require("assert/strict");
const { chromium } = require("playwright");

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = cfg.out;
const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const matrix = [];
const problems = [];
const [, , w3] = cfg.weeks;

async function check(name, fn) {
  try {
    await fn();
    matrix.push({ check: name, result: "pass" });
  } catch (e) {
    matrix.push({ check: name, result: "FAIL", detail: String(e && e.message ? e.message : e).slice(0, 700) });
  }
}

async function axe(page, name) {
  await page.evaluate(AXE);
  const found = await page.evaluate(async () => {
    const r = await window.axe.run(document, { resultTypes: ["violations"] });
    return r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(" ")) }));
  });
  const bad = found.filter((v) => v.impact === "serious" || v.impact === "critical");
  for (const v of bad) problems.push(`${name}: ${v.id} (${v.impact}) ${v.nodes.slice(0, 3).join(", ")}`);
  return bad;
}

// 390 px and 1,280 px, light and dark: axe, no sideways scroll at 390, a screenshot. `keep`: a dialog is open (no
// blur, no scroll), so the shot shows it.
async function shoot(page, name, keep = false) {
  if (!keep) await page.evaluate(() => { document.activeElement?.blur(); scrollTo(0, 0); });
  for (const [w, h, tag] of [[390, 844, "390"], [1280, 900, "desktop"]]) {
    await page.setViewportSize({ width: w, height: h });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.waitForTimeout(120);
      await axe(page, `${name} ${tag} ${scheme}`);
      if (w === 390) {
        const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        if (over > 0) problems.push(`${name} 390: the page scrolls sideways by ${over}px`);
      }
      await page.screenshot({ path: `${out}/${name}-${tag}-${scheme}.png`, fullPage: !keep });
    }
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
}

async function settled(page) {
  await page.waitForSelector("section[data-part][aria-busy='false']", { timeout: 40000 });
}

async function ctl(path) {
  const r = await fetch(`${cfg.control}${path}`);
  assert.equal(r.status, 200);
}

// The outcome line: waits until it says what `re` expects (an earlier outcome may still be showing).
async function toast(page, re) {
  await page.waitForSelector("[data-result]:not([hidden])", { timeout: 15000 });
  try {
    await page.waitForFunction((src) => new RegExp(src).test(document.querySelector("[data-result-text]").textContent), re.source, { timeout: 15000 });
  } catch {
    assert.match(await page.textContent("[data-result-text]"), re);
  }
  return page.textContent("[data-result-text]");
}

async function open(browser, server, path, viewport = { width: 1280, height: 900 }) {
  const ctx = await browser.newContext({ viewport });
  await ctx.addCookies([{ name: "admin_session", value: cfg.cookies[server], url: cfg[server] }]);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(`${server}: ${e}`));
  await page.goto(`${cfg[server]}${path}`);
  await settled(page);
  return { ctx, page };
}

// Open a job's form on the Jobs page, fill it, press Start…, and wait for the confirm sheet's plan.
async function startForm(page, action, fill = {}) {
  await page.goto(`${cfg.act}/jobs#${action}`);
  await settled(page);
  const d = page.locator(`details#${action}`);
  if (!(await d.getAttribute("open"))) await d.locator("summary").click();
  for (const [k, v] of Object.entries(fill)) {
    const el = d.locator(`[name=${k}]`);
    if ((await el.evaluate((e) => e.tagName)) === "SELECT") await el.selectOption(v);
    else await el.fill(v);
  }
  await d.locator("button[type=submit]").click();
  await page.waitForSelector("dialog#confirm[open] .steps, dialog#confirm[open] .refused", { timeout: 15000 });
}

const errors = [];

(async () => {
  const browser = await chromium.launch();
  const { page } = await open(browser, "act", "/jobs");
  let roaster = null;
  let servedBefore = null;

  try {
    await check("Jobs: nothing started yet (empty), every job listed with its help", async () => {
      assert.match(await page.textContent("[data-answer]"), /0 running · 0 started from here/);
      for (const label of ["Crawl", "Crawl one roaster", "Shopify retry", "Fetch health", "Merge", "Enrich", "Classify", "Build"]) {
        assert.ok(await page.isVisible(`.jname:text-is("${label}")`), label);
      }
      await shoot(page, "jobs-empty");
    });

    await check("Catalog: exclude one coffee (confirm with a reason), then Undo from the outcome", async () => {
      await page.goto(`${cfg.act}/catalog?q=onyx`);
      await settled(page);
      const first = page.locator("input[data-pick] >> nth=0");
      roaster = JSON.parse(await first.getAttribute("data-pick")).roaster_id;
      await first.check(); // one ticked row: the bulk bar acts on it
      await page.click("[data-bulk] form[data-picks] button >> nth=0");
      await page.waitForSelector("dialog#confirm[open] [data-ask-reason]:not([hidden])");
      assert.match(await page.textContent("dialog#confirm [data-confirm-title]"), /Exclude this product\?/);
      await shoot(page, "catalog-decide-dialog", true);
      await page.click("[data-confirm-go]");
      await page.waitForFunction(() => /reason/i.test(document.querySelector("[data-confirm-hint]").textContent)); // a reason is required
      await page.fill("#confirm-reason", "a drink mix, not coffee");
      await page.click("[data-confirm-go]");
      await toast(page, /Recorded 1 decision/);
      await settled(page);
      assert.ok(await page.locator("table form[data-write$='/undo']").count());
      await shoot(page, "catalog-decided");
      await page.click("[data-result] [data-undo]");
      await toast(page, /Undone \(1\)/);
      await page.waitForFunction(() => !document.querySelector("table form[data-write$='/undo']"));
      await shoot(page, "catalog-undone");
    });

    await check("Catalog: choose a group, exclude it by version hash, then undo one of it from its row", async () => {
      await page.check("input[data-pick] >> nth=0");
      await page.check("input[data-pick] >> nth=1");
      assert.match(await page.textContent("[data-bulk-count]"), /2 chosen/);
      await shoot(page, "catalog-chosen");
      await page.click("[data-bulk] form[data-picks] button >> nth=0");
      await page.waitForSelector("dialog#confirm[open] [data-ask-kind]:not([hidden])");
      assert.match(await page.textContent("dialog#confirm [data-confirm-title]"), /Exclude 2 products/);
      await page.check("dialog#confirm input[name=confirm-kind][value=input_hash]");
      await page.fill("#confirm-reason", "the same pasted sample-pack text");
      await page.click("[data-confirm-go]");
      await toast(page, /Recorded 2 decisions/);
      await settled(page);
      assert.equal(await page.locator("text=(group)").count(), 2);
      await shoot(page, "catalog-group");
    });

    await check("Release: decisions not in the served release; build a draft (confirm), then it is building", async () => {
      await page.goto(`${cfg.act}/release`);
      await settled(page);
      assert.match(await page.textContent("[data-answer]"), /2 decision changes not in it/);
      await page.click("form[data-write='/api/drafts'] button");
      await page.waitForSelector("dialog#confirm[open]");
      assert.match(await page.textContent("[data-confirm-body]"), /--draft/);
      await shoot(page, "release-draft-confirm", true);
      await page.click("[data-confirm-go]");
      await toast(page, /Started build/);
      await settled(page);
      assert.match(await page.textContent("main"), /building/);
      await shoot(page, "release-building");
    });

    await check("Release: the draft built; Publish is off until the live check, and says why", async () => {
      await ctl("/finish?job=build");
      await page.reload();
      await settled(page);
      assert.match(await page.textContent("[data-answer]"), /built/);
      assert.match(await page.textContent(".checks"), /no answered test crawl/);
      const pub = page.locator("button:text('Publish…')");
      assert.equal(await pub.getAttribute("aria-disabled"), "true");
      assert.match(await page.textContent("main"), /decision:exclude/); // the draft's changes, with their reason
      await shoot(page, "release-not-ready");
    });

    await check("Jobs: a live check (crawl one roaster, test mode): the confirm shows the exact arguments, then it runs", async () => {
      await startForm(page, "crawl-one", { connector: "shopify", roaster });
      const steps = await page.textContent("dialog#confirm .steps");
      assert.match(steps, new RegExp(`Cloud Run job crawl-shopify\\s*crawl --connector shopify --only ${roaster.replace(/\./g, "\\.")} --test t-`));
      assert.match(await page.textContent("dialog#confirm [data-confirm-title]"), /Start Crawl one roaster\?/);
      await shoot(page, "jobs-confirm", true);
      await page.click("[data-confirm-go]");
      await toast(page, /Started crawl-shopify/);
      await settled(page);
      assert.ok(await page.isVisible("[data-poll]"));
      assert.match(await page.textContent("[data-answer]"), /1 running/);
      assert.ok(await page.isVisible("a.ev:text('logs')"));
      await shoot(page, "jobs-running");
    });

    await check("Jobs: the same roaster again is refused (once a day), with Confirm off and the reason shown", async () => {
      await startForm(page, "crawl-one", { connector: "shopify", roaster });
      assert.match(await page.textContent("dialog#confirm"), /in the last day/);
      assert.equal(await page.getAttribute("[data-confirm-go]", "aria-disabled"), "true");
      await shoot(page, "jobs-refused", true);
      await page.click("dialog#confirm [data-close]");
    });

    await check("Jobs: a running job can't start twice (no overlap)", async () => {
      await startForm(page, "crawl", { connectors: "shopify" });
      assert.match(await page.textContent("dialog#confirm"), /crawl-shopify is already running/);
      await page.click("dialog#confirm [data-close]");
    });

    await check("Jobs: success, polled in place; the test crawl says the store answered", async () => {
      await page.goto(`${cfg.act}/jobs`);
      await settled(page);
      await ctl("/finish?job=crawl-shopify");
      await page.waitForFunction(() => !document.querySelector("[data-runs]")?.textContent.includes("running"), null, { timeout: 15000 });
      assert.match(await page.textContent("[data-runs] tbody tr >> nth=0"), /succeeded[\s\S]*store: answered/);
      await shoot(page, "jobs-success");
    });

    await check("Jobs: a double click on Confirm starts one run", async () => {
      await startForm(page, "fetch-health");
      await page.dblclick("[data-confirm-go]");
      await toast(page, /Started fetch-health/);
      await settled(page);
      assert.equal(await page.locator("[data-runs] tbody tr", { hasText: "fetch-health" }).count(), 1);
    });

    await check("Jobs: failure, with Cloud Run's message", async () => {
      await ctl("/finish?job=fetch-health&status=failed&message=Task%20failed%3A%20exit%201");
      await page.reload();
      await settled(page);
      assert.match(await page.textContent("[data-runs] tr.bad"), /failed[\s\S]*Task failed: exit 1/);
      await shoot(page, "jobs-failed");
    });

    await check("Jobs: cancel a running job (a destructive confirm)", async () => {
      await startForm(page, "merge", { week: w3 });
      await page.click("[data-confirm-go]");
      await toast(page, /Started merge/);
      await settled(page);
      await page.click("[data-runs] form[data-write$='/cancel'] button");
      await page.waitForSelector("dialog#confirm[open]");
      assert.ok(await page.locator("[data-confirm-go].danger").count());
      await shoot(page, "jobs-cancel-confirm", true);
      await page.click("[data-confirm-go]");
      await page.waitForFunction(() => document.querySelector("[data-runs]")?.textContent.includes("cancelled"), null, { timeout: 15000 });
      await shoot(page, "jobs-cancelled");
    });

    await check("Jobs: Cloud Run refuses a start: nothing is pretended, the outcome says so", async () => {
      await ctl("/refuse?job=enrich-week");
      await startForm(page, "enrich-week", { week: w3 });
      await page.click("[data-confirm-go]");
      await toast(page, /Nothing started\. Not started: enrich-week: Cloud Run refused \(403\)/);
      await ctl("/refuse");
    });

    await check("Release: the live check holds, Publish (confirm), publishing, then published", async () => {
      await page.goto(`${cfg.act}/release`);
      await settled(page);
      assert.doesNotMatch(await page.textContent(".checks"), /no answered test crawl/);
      servedBefore = (await page.textContent("[data-answer]")).match(/Serving (\S+)/)[1];
      await page.click("button:text('Publish…')");
      await page.waitForSelector("dialog#confirm[open]");
      await shoot(page, "release-publish-confirm", true);
      await page.click("[data-confirm-go]");
      await toast(page, /Started publish/);
      await settled(page);
      assert.match(await page.textContent("main"), /publishing/i);
      await shoot(page, "release-publishing");
      await ctl("/finish?job=publish");
      await page.reload();
      await settled(page);
      assert.match(await page.textContent("[data-answer]"), /published/);
      assert.match(await page.textContent("main"), /Served now/);
      await shoot(page, "release-published");
    });

    await check("Release: roll back to the release served before (a destructive confirm with a reason)", async () => {
      await page.goto(`${cfg.act}/release?build=${servedBefore}`);
      await settled(page);
      await page.click("form[data-write$='/rollback'] button");
      await page.waitForSelector("dialog#confirm[open] [data-ask-reason]:not([hidden])");
      await page.fill("#confirm-reason", "the publish removed the wrong coffee");
      await page.click("[data-confirm-go]");
      await toast(page, /Started publish/);
      await ctl("/finish?job=publish");
      await page.reload();
      await settled(page);
      assert.match(await page.textContent("main"), /Served now/);
      await shoot(page, "release-rolled-back");
    });

    await check("Release: a draft whose build fails, then Discard", async () => {
      await page.goto(`${cfg.act}/release`);
      await settled(page);
      await page.click("form[data-write='/api/drafts'] button");
      await page.click("[data-confirm-go]");
      await toast(page, /Started build/);
      await ctl("/finish?job=build&status=failed&message=dbt%20test%20failed");
      await page.reload();
      await settled(page);
      assert.match(await page.textContent("table[aria-label], .frame[aria-label=Drafts]"), /failed/);
      await page.click(".frame[aria-label=Drafts] form[data-write$='/discard'] button >> nth=0");
      await page.click("[data-confirm-go]");
      await settled(page);
      await page.waitForFunction(() => document.querySelector(".frame[aria-label=Drafts]")?.textContent.includes("discarded"));
      await shoot(page, "release-discarded");
    });

    await check("Log: every write, filterable, with the decisions download", async () => {
      await page.goto(`${cfg.act}/log`);
      await settled(page);
      const all = await page.textContent("[data-answer]");
      assert.match(all, /\d+ writes recorded/);
      for (const a of ["job start", "job cancel", "decide", "undo", "discard"]) assert.match(await page.textContent("table"), new RegExp(a));
      await shoot(page, "log");
      await page.selectOption("#action", "decide");
      await page.waitForURL(/action=decide/);
      await settled(page);
      assert.match(await page.textContent("[data-answer]"), /2 decides recorded/);
      const r = await page.request.get(`${cfg.act}/api/decisions/export`);
      assert.equal(r.status(), 200);
      assert.equal((await r.json()).version, 1);
    });

    await check("The Monday window: refused, then started with a recorded reason", async () => {
      const { ctx, page: p } = await open(browser, "window", "/jobs#merge");
      if (!(await p.locator("details#merge").getAttribute("open"))) await p.click("details#merge summary");
      await p.click("details#merge button[type=submit]");
      await p.waitForSelector("dialog#confirm[open] .notice");
      assert.match(await p.textContent("dialog#confirm"), /Monday crawl window/);
      await p.click("[data-confirm-go]");
      await p.waitForFunction(() => document.querySelector("[data-confirm-hint]").textContent.includes("override"));
      await shoot(p, "jobs-window", true);
      await p.fill("#confirm-override", "re-run the merge after a 429 storm");
      await p.click("[data-confirm-go]");
      await toast(p, /Started merge/);
      await settled(p);
      assert.match(await p.textContent("[data-runs]"), /window overridden/);
      await ctx.close();
    });

    await check("A slow Cloud Run: the Confirm button shows it is working, then the outcome", async () => {
      const { ctx, page: p } = await open(browser, "slow", "/jobs#fetch-health", { width: 390, height: 844 });
      if (!(await p.locator("details#fetch-health").getAttribute("open"))) await p.click("details#fetch-health summary");
      await p.click("details#fetch-health button[type=submit]");
      await p.waitForSelector("dialog#confirm[open] .steps");
      await p.click("[data-confirm-go]");
      await p.waitForSelector("[data-confirm-go][aria-disabled=true] .cup");
      await p.screenshot({ path: `${out}/jobs-slow-390-light.png` });
      await axe(p, "jobs-slow 390 light");
      await toast(p, /Started fetch-health/);
      await ctx.close();
    });

    await check("Writes off: the pages read, the actions say why", async () => {
      const { ctx, page: p } = await open(browser, "off", "/catalog");
      assert.match(await p.textContent("main"), /Actions are off: no control-plane database/);
      assert.equal(await p.locator("[data-bulk], input[data-pick]").count(), 0);
      await shoot(p, "writes-off");
      await ctx.close();
    });

    await check("The control plane is down: the page degrades (no actions, says why), and a write is refused with nothing started", async () => {
      const { ctx, page: p } = await open(browser, "down", "/jobs");
      assert.match(await p.textContent("main"), /control plane can.t be read right now/); // degraded, not blank
      assert.equal(await p.locator("form[data-write]").count(), 0);
      await shoot(p, "control-plane-down");
      const r = await p.evaluate(async () => (await fetch("/api/jobs/merge/runs", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" }, body: JSON.stringify({ key: "k" }) })).status);
      assert.equal(r, 503);
      await ctx.close();
    });

    await check("CSRF: a write without the same-origin markers is refused", async () => {
      const r = await page.evaluate(async () => (await fetch("/api/decisions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: "k" }) })).status);
      assert.equal(r, 403);
    });

    await check("Signed out: a write sends you to sign in, saying the session ended", async () => {
      await page.goto(`${cfg.act}/catalog`);
      await settled(page);
      await page.context().clearCookies();
      await page.check("input[data-pick] >> nth=0");
      await page.click("[data-bulk] form[data-picks] button >> nth=0");
      await page.fill("#confirm-reason", "this will not be recorded");
      const ended = page.waitForRequest((r) => r.url().includes("/signin?ended=1"));
      await page.click("[data-confirm-go]");
      await ended; // (this test service has no passkey, so sign-in then forwards to enrollment)
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
