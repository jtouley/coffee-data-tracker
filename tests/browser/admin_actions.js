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

// Run a job (the button beside the title): pick the job, fill its fields, press Run…, and wait for the confirm's plan.
async function openRun(page, action, fill = {}) {
  await page.click("[data-title-actions] button:text-is('Run a job')");
  await page.waitForSelector("dialog#run-job[open]");
  await page.selectOption("dialog#run-job [data-job]", action);
  for (const [k, v] of Object.entries(fill)) {
    const el = page.locator(`dialog#run-job [name=${k}]`);
    if ((await el.evaluate((e) => e.tagName)) === "SELECT") await el.selectOption(v);
    else await el.fill(v);
  }
}

async function startForm(page, action, fill = {}) {
  await page.goto(`${cfg.act}/jobs`);
  await settled(page);
  await openRun(page, action, fill);
  await page.click("dialog#run-job button[type=submit]");
  await page.waitForSelector("dialog#confirm[open] .steps, dialog#confirm[open] .refused", { timeout: 15000 });
}

const errors = [];

(async () => {
  const browser = await chromium.launch();
  const { page } = await open(browser, "act", "/jobs");
  let roaster = null;
  let servedBefore = null;

  try {
    await check("Jobs: nothing started yet (empty); one Run a job button and a ? help beside the title", async () => {
      assert.match(await page.textContent("[data-answer]"), /0 running · 0 started from here/);
      await shoot(page, "jobs-empty");
      await page.click("[data-title-actions] button.help");
      await page.waitForSelector("dialog#help-jobs[open]");
      const help = await page.textContent("dialog#help-jobs");
      for (const label of ["Crawl", "Crawl one roaster", "Shopify retry", "Fetch health", "Merge", "Enrich", "Classify", "Build", "Site"]) {
        assert.match(help, new RegExp(label), label);
      }
      await shoot(page, "jobs-help", true);
      await page.keyboard.press("Escape");
      await openRun(page, "crawl", { connectors: "shopify" });
      assert.ok(await page.isHidden("dialog#run-job [data-field=roaster]"), "only the chosen job's fields");
      assert.match(await page.textContent("dialog#run-job [data-job-hint]"), /Every store on one platform/);
      await shoot(page, "jobs-run-sheet", true);
      await page.selectOption("dialog#run-job [data-job]", "site");
      assert.ok(await page.isVisible("dialog#run-job [data-site] a"), "Site is a link to GitHub");
      await page.keyboard.press("Escape");
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
      assert.equal(await page.locator("[data-runs] tbody tr", { hasText: "Fetch health" }).count(), 1);
    });

    await check("Jobs: failure, with Cloud Run's message", async () => {
      await ctl("/finish?job=fetch-health&status=failed&message=Task%20failed%3A%20exit%201");
      await page.reload();
      await settled(page);
      assert.match(await page.textContent("[data-runs] tbody tr >> nth=0"), /failed[\s\S]*Task failed: exit 1/);
      await shoot(page, "jobs-failed");
    });

    await check("Jobs: cancel a running job (a destructive confirm)", async () => {
      await startForm(page, "merge", { week: w3 });
      await page.click("[data-confirm-go]");
      await toast(page, /Started merge/);
      await settled(page);
      await page.click("[data-runs] tbody tr >> nth=0 >> summary"); // Cancel is in the run's ⋯
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

    await check("Queue: open flags by product, the evidence group first, one line each", async () => {
      await page.goto(`${cfg.act}/queue`);
      await settled(page);
      assert.match(await page.textContent("[data-answer]"), /3 products with open flags · 2 ready to remove/);
      assert.match(await page.textContent("tr.grp"), /2 products of [^ ]+ with the same evidence: non_coffee_type, verdict_flip · 2 supports · ready/);
      assert.match(await page.textContent("main"), /1 doubt · 1 restore/);
      await shoot(page, "queue");
    });

    await check("Queue: a case shows each flag's polarity and evidence, the listing, the fetches and the store", async () => {
      await page.click("table.flagged tbody tr:not(.grp) th a >> nth=2");
      await settled(page);
      const text = await page.textContent("main");
      for (const re of [/shared_body/, /doubts/, /restore/, /this sample pack includes three bags/, /Crawl flag/, /Live check/, /fetches, the last/]) assert.match(text, re);
      await shoot(page, "case");
    });

    await check("Case: turn the store's crawl flag off (a confirm with a reason)", async () => {
      await page.click("form[data-write$='/crawl'] button[type=submit]");
      await page.waitForSelector("dialog#confirm[open] #confirm-reason");
      assert.match(await page.textContent("dialog#confirm"), /Turn the crawl flag off/);
      await page.fill("#confirm-reason", "sells no coffee: drink mixes only");
      await page.click("[data-confirm-go]");
      await toast(page, /Crawl flag off/);
      await settled(page);
      assert.match(await page.textContent("main"), /sells no coffee: drink mixes only/);
    });

    await check("Queue: exclude the evidence group in one decision; its rows then say next draft", async () => {
      await page.goto(`${cfg.act}/queue`);
      await settled(page);
      await page.click("tr.grp button[type=submit]");
      await page.waitForSelector("dialog#confirm[open] #confirm-reason");
      assert.match(await page.textContent("dialog#confirm [data-confirm-title]"), /Exclude 2 products/);
      await shoot(page, "queue-group-confirm", true);
      await page.fill("#confirm-reason", "drink mixes: two families agree");
      await page.click("[data-confirm-go]");
      await toast(page, /Recorded 2 decisions/);
      await settled(page);
      assert.equal(await page.locator("text=excluded · next draft").count(), 2);
    });

    await check("Queue: a settings edit that changes how a rule decides is checked first, and says it needs its evaluation", async () => {
      await page.click("details.settings summary");
      const doc = await page.inputValue("#settings-doc");
      await page.fill("#settings-doc", doc.replace('"added": "supports"', '"added": "doubts"'));
      await page.fill("#settings-reason", "a re-added coffee only doubts a removal");
      await page.click("form.settings-form button[type=submit]");
      await page.waitForFunction(() => document.querySelector("dialog#confirm[open] [data-confirm-body]")?.textContent.includes("full evaluation"));
      await shoot(page, "settings-check", true);
      await page.click("[data-confirm-go]");
      await toast(page, /Saved rule settings/);
      await settled(page);
      assert.match(await page.textContent("details.settings summary"), /version/);
    });

    await check("Queue: settings the code can't read are refused, and nothing is saved", async () => {
      if (!(await page.locator("details.settings").getAttribute("open"))) await page.click("details.settings summary");
      await page.fill("#settings-doc", '{"schema": 1, "rules": []');
      await page.fill("#settings-reason", "a broken edit");
      await page.click("form.settings-form button[type=submit]");
      await page.waitForFunction(() => /valid JSON/.test(document.querySelector("dialog#confirm[open] [data-confirm-body]")?.textContent || ""));
      assert.equal(await page.getAttribute("[data-confirm-go]", "aria-disabled"), "true");
      await page.click("dialog#confirm [data-close]");
    });

    await check("Sources: search first, one Filter / Sort panel, compact rows with a quiet status and a ⋯ menu", async () => {
      await page.goto(`${cfg.act}/sources`);
      await settled(page);
      const main = await page.textContent("main");
      assert.match(main, /to review of .* places/);
      assert.match(main, /roasters from the registry · 0 flagged as roasters/);
      assert.match(main, /Unreviewed/);
      assert.equal(await page.locator("table.sources tbody form[data-now]:visible").count(), 0, "no action button on a row");
      assert.ok(await page.evaluate(() => document.querySelector("form.search").compareDocumentPosition(document.querySelector("[data-answer]")) & Node.DOCUMENT_POSITION_FOLLOWING), "the search comes first");
      await shoot(page, "sources");
      await page.setViewportSize({ width: 390, height: 844 });
      for (const sel of ["[data-panel]", ".tabs a.on", "table.sources tbody summary >> nth=0"]) { // on screen at 390, 44 px targets
        const box = await page.locator(sel).first().boundingBox();
        assert.ok(box && box.x >= 0 && box.x + box.width <= 390 && box.height >= 44, `${sel} at ${JSON.stringify(box)}`);
      }
      await page.setViewportSize({ width: 320, height: 640 }); // reflow at 320 px (WCAG 1.4.10): no sideways scroll
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth), 0);
      await page.setViewportSize({ width: 1280, height: 900 });
      await page.click("[data-panel]");
      await page.waitForSelector("dialog#filters[open]");
      await shoot(page, "sources-panel", true);
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => !document.querySelector("dialog#filters").open);
      await page.click("[data-panel]");
      assert.ok(await page.isHidden("dialog#filters fieldset.sorts"), "a desktop sorts by the table's headers");
      await page.selectOption("dialog#filters select[name=tag]", "coffee_shop");
      await page.click("dialog#filters button[type=submit]");
      await settled(page);
      assert.match(await page.textContent("[data-panel]"), /Filter \/ Sort \(1\)/);
      assert.match(await page.textContent("[data-answer-matching]"), / of .* places match/);
    });

    await check("Sources (desktop): a data table; a header sorts by its column, both ways, and says so (aria-sort)", async () => {
      await page.goto(`${cfg.act}/sources`);
      await settled(page);
      const heads = await page.$$eval("table.sources thead th", (ths) => ths.filter((t) => t.offsetParent && !t.querySelector(".vh")).map((t) => t.textContent.replace(/[↑↓]/g, "").trim()));
      assert.deepEqual(heads.filter(Boolean), ["Name", "Domain", "City", "State", "Tag", "Hints", "Status", "First seen"]);
      const row = await page.locator("table.sources tbody tr >> nth=0").boundingBox();
      assert.ok(row.height >= 44 && row.height < 56, `a dense row, ${row.height}px`);
      await page.click("table.sources thead a.sort:has-text('First seen')");
      await settled(page);
      assert.equal(await page.getAttribute("table.sources thead th[aria-sort]", "aria-sort"), "ascending");
      await page.click("table.sources thead a.sort:has-text('First seen')");
      await settled(page);
      assert.equal(await page.getAttribute("table.sources thead th[aria-sort]", "aria-sort"), "descending");
      assert.match(await page.textContent("table.sources thead th[aria-sort]"), /First seen↓/);
      await shoot(page, "sources-sorted");
      await page.evaluate(() => scrollTo(0, 2000)); // the header stays in view
      const top = await page.locator("table.sources thead th >> nth=1").boundingBox();
      assert.ok(top.y >= -1 && top.y < 60, `sticky header at ${top.y}`);
    });

    await check("Sources: a row's ⋯ menu flags one place Roaster (keyboard too), then Undo from the outcome", async () => {
      await page.goto(`${cfg.act}/sources?q=${encodeURIComponent("https://www.ember-roastworks.example/shop")}`); // a pasted address finds its domain
      await settled(page);
      assert.match(await page.textContent("[data-answer-matching]"), /^1 of /);
      assert.match(await page.textContent("table.sources"), /roast in the name/);
      await page.focus("table.sources summary");
      await page.keyboard.press("Enter");
      await page.waitForSelector("details[data-rowmenu][open]");
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => !document.querySelector("details[data-rowmenu][open]"));
      assert.ok(await page.evaluate(() => document.activeElement.matches("details[data-rowmenu] summary")), "focus back on ⋯");
      await page.click("table.sources summary");
      await shoot(page, "sources-menu", true);
      await page.click("details[data-rowmenu][open] button:text-is('Roaster')");
      await toast(page, /Marked 1 place as a roaster\./);
      await settled(page);
      assert.match(await page.textContent("table.sources"), /Roaster/);
      await page.click("[data-result] [data-undo]");
      await toast(page, /Undone \(1\)/);
      await settled(page);
      assert.match(await page.textContent("table.sources"), /Unreviewed/);
      await page.click("table.sources summary");
      await page.click("details[data-rowmenu][open] button:text-is('Roaster')");
      await toast(page, /^Marked 1 place as a roaster\.$/);
      await page.waitForFunction(() => /Roaster/.test(document.querySelector("table.sources td.c-status")?.textContent || "") && !/Unreviewed/.test(document.querySelector("table.sources td.c-status").textContent));
    });

    await check("Sources: Pull now from a row's ⋯ menu: the Jobs confirm in plain words, one job (probe, then a counted crawl)", async () => {
      await page.click("table.sources summary");
      await page.click("details[data-rowmenu][open] button:text-is('Pull now')");
      await page.waitForSelector("dialog#confirm[open] .plan-details");
      assert.ok(await page.isHidden("[data-result]"), "the outcome line steps aside for the confirm");
      assert.match(await page.textContent("dialog#confirm [data-confirm-title]"), /^Probe and crawl 1 place\?$/);
      assert.match(await page.textContent("dialog#confirm [data-confirm-body]"), /^ember-roastworks\.example\. One job probes them, then crawls/);
      assert.ok(!(await page.isVisible("dialog#confirm .steps")), "the command waits behind Details");
      await shoot(page, "sources-pull-confirm", true);
      await page.click("dialog#confirm .plan-details summary");
      assert.match(await page.textContent("dialog#confirm .steps"), /coffeedata pull --only ember-roastworks\.example --run-date/);
      await page.click("[data-confirm-go]");
      await toast(page, /Started pull/);
      await ctl("/finish?job=pull");
    });

    await check("Sources: select two places (tap a row, tick a box), then Not a roaster from the action bar", async () => {
      await page.goto(`${cfg.act}/sources?status=unreviewed`);
      await settled(page);
      await page.click("table.sources tbody tr >> nth=0 >> .pname"); // a tap on the row selects it
      await page.check("input[data-pick] >> nth=1");
      assert.match(await page.textContent("[data-bulk-count]"), /2 selected/);
      await shoot(page, "sources-selected", true);
      await page.click("[data-bulk] button:text-is('Not a roaster')");
      await toast(page, /Marked 2 places as not roasters\./);
      await settled(page);
      assert.match(await page.textContent("[data-counts]"), /1 flagged as a roaster · 2 flagged as not roasters/);
    });

    await check("Release: the draft carries the rule settings and the crawl flag; Publish waits for the change's evaluation", async () => {
      await page.goto(`${cfg.act}/release`);
      await settled(page);
      assert.match(await page.textContent("[data-answer]"), /rule settings, 1 crawl flag, 1 source flag not in it/);
    });

    await check("The Monday window: refused, then started with a recorded reason", async () => {
      const { ctx, page: p } = await open(browser, "window", "/jobs");
      await openRun(p, "merge");
      await p.click("dialog#run-job button[type=submit]");
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
      const { ctx, page: p } = await open(browser, "slow", "/jobs", { width: 390, height: 844 });
      await openRun(p, "fetch-health");
      await p.click("dialog#run-job button[type=submit]");
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
