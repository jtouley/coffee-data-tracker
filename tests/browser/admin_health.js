// Week's crawl health in a browser (tests/test_admin_health_browser.py starts the servers and writes the config):
// every state at 390 px and 1,280 px, light and dark, with axe and no sideways scroll at 390 px. Every check runs
// even if an earlier one failed; the matrix is printed and saved as matrix.json.
// Usage: node admin_health.js <config.json>
"use strict";
const fs = require("fs");
const assert = require("assert/strict");
const { chromium } = require("playwright");

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = cfg.out;
const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const matrix = [];
const problems = [];
const [w0, w1, w2, w3] = cfg.weeks;

async function check(name, fn) {
  try {
    await fn();
    matrix.push({ check: name, result: "pass" });
  } catch (e) {
    matrix.push({ check: name, result: "FAIL", detail: String(e && e.message ? e.message : e).slice(0, 600) });
  }
}

async function axe(page, name) {
  await page.evaluate(AXE);
  const found = await page.evaluate(async () => {
    const r = await window.axe.run(document, { resultTypes: ["violations"] });
    return r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(" ")) }));
  });
  for (const v of found) problems.push(`${name}: ${v.id} (${v.impact}) ${v.nodes.slice(0, 3).join(", ")}`);
  return found; // any violation, not only serious ones
}

async function shoot(page, name, { open = [] } = {}) {
  for (const sel of open) await page.evaluate((s) => document.querySelectorAll(s).forEach((d) => { d.open = true; }), sel);
  await page.evaluate(() => { document.activeElement?.blur(); scrollTo(0, 0); });
  const bad = [];
  await page.setViewportSize({ width: 320, height: 700 }); // the narrowest: nothing may scroll sideways
  const over320 = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (over320 > 0) problems.push(`${name} 320: the page scrolls sideways by ${over320}px`);
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
      if (w === 390) await page.screenshot({ path: `${out}/${name}-${tag}-${scheme}-top.png` }); // the first screen
    }
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  assert.deepEqual(bad.map((v) => v.id), [], `axe: ${name}`);
}

async function settled(page) {
  await page.waitForSelector("section[data-part][aria-busy='false']", { timeout: 40000 });
}

const text = async (page, sel) => (await page.innerText(sel)).replace(/\s+/g, " ").trim();

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  async function open(app, path, viewport = { width: 1280, height: 900 }) {
    const ctx = await browser.newContext({ viewport });
    await ctx.addCookies([{ name: "admin_session", value: cfg.cookies[app], url: cfg[app] }]);
    const page = await ctx.newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`${cfg[app]}${path}`);
    return { ctx, page };
  }

  try {
    await check("this week: a died attempt, deadlines, jumps and refusals flagged under the answer", async () => {
      const { ctx, page } = await open("main", `/week?week=${w3}`);
      await settled(page);
      const a = await text(page, "[data-answer]");
      assert.match(a, /^5 of 5 connectors landed · 7 things to look at · Tuesday’s merge hasn’t run yet$/);
      const look = await text(page, "ul.flags.health");
      assert.match(look, /generic’s resume crawl died\. It started Mon 23 Nov 15:20 UTC, 8\.2 hours ago, and never reported finishing\./);
      assert.match(look, /A fetch-health check failed/);
      assert.match(look, /woocommerce hit its time limit in the morning and resume passes, as last week\. The stores it didn’t reach or finish weren’t read whole this week\./);
      assert.match(look, /squarespace read 2 catalogs only in part in its retry pass\. The products it didn’t read stay listed as they were\. big-catalog\.example was partial in each of its last 2 runs, so never read whole\./);
      assert.equal(await page.locator("ul.flags.health li.usual").count(), 2);  // the usual time limit and the partial catalogs: calm, listed last
      assert.equal(await page.locator("ul.flags.health li").last().getAttribute("class"), "usual");
      assert.match(look, /generic: errors up from 71 to 120 and 429s up from 83 to 152/);
      assert.match(look, /No retry crawl for woocommerce\. It was due at 21:47 UTC, and the other connectors ran theirs\./);
      assert.match(look, /9% of stores refused our first request from 203\.0\.113\.40 \(92 of 1,010, generic, woocommerce\)/);
      // failures first: each failure's mark is the square, in --danger
      assert.equal(await page.locator("ul.flags.health li.bad").count(), 3);
      assert.equal(await page.locator("ul.flags.health li").first().getAttribute("class"), "bad");
      const vs = await text(page, "table.vs");
      assert.match(vs, /square_online running 184 7 0 0 0 of 191 /); // counted to its resume, no change: its retry is running
      assert.match(vs, /All 2,[0-9]{3} /);
      assert.match(await text(page, "#passes ~ .frame"), /Resume · 15:17 died/);
      assert.match(await text(page, "#passes ~ .frame"), /Retry · 21:47 running/);
      assert.equal(await page.locator("tr.bad", { hasText: "Resume" }).count(), 1);
      assert.match(await text(page, "#tuesday + p"), /^Not yet: the merge starts Tuesday 03:17 UTC/);
      assert.match(await text(page, "#passes ~ .frame"), /Resume · 15:17 died Mon 23 Nov 15:20 30 4 28 5 of 5 \(1 died\)/);
      assert.match(await text(page, "#passes ~ .frame"), /Retry · 21:47 running .* 4 of 5 \(1 still running\)/);
      assert.equal(await page.locator("a", { hasText: "Test crawl" }).count(), 0); // writes are off here
      await shoot(page, "health-this-week");
      // The attempts fold open: the died attempt is marked as one
      await page.click("details.attempts > summary");
      assert.equal(await page.locator("details.attempts tr.bad", { hasText: "died" }).count(), 1);
      await shoot(page, "health-this-week-attempts", { open: ["details.attempts"] });
      await ctx.close();
    });

    await check("a clean week: nothing unusual, Tuesday published, no Needs a look", async () => {
      const { ctx, page } = await open("main", `/week?week=${w1}`);
      await settled(page);
      assert.equal(await text(page, "[data-answer]"), "5 of 5 connectors landed · nothing unusual · published");
      assert.equal(await page.locator("#look").count(), 0);
      assert.match(await text(page, "#tuesday ~ dl.facts"), /Merge done .* Enrich done .* Build passed .* Publish published/);
      await shoot(page, "health-clean-week");
      await ctx.close();
    });

    await check("last week: compared with the week before, the deadline flagged, served now", async () => {
      const { ctx, page } = await open("main", `/week?week=${w2}`);
      await settled(page);
      assert.match(await text(page, "[data-answer]"), /1 thing to look at · published/);
      const look = await text(page, "ul.flags.health");
      assert.match(look, /woocommerce hit its time limit in the morning, resume and retry passes/);
      assert.match(look, /retry passes\. The stores it didn’t reach or finish weren’t read whole this week\./);
      await shoot(page, "health-last-week");
      await ctx.close();
    });

    await check("a week with fetches but no job rows: its empty state, and a link to the latest", async () => {
      const { ctx, page } = await open("main", `/week?week=${w0}`);
      await settled(page);
      assert.equal(await page.getAttribute("[data-state]", "data-state"), "empty");
      assert.match(await text(page, ".state.empty"), new RegExp(`Nothing for week ${w0} yet`));
      assert.equal(await page.getAttribute(`a[href='/week?week=${w3}']`, "href"), `/week?week=${w3}`);
      await shoot(page, "health-empty-week");
      await ctx.close();
    });

    await check("no weeks in the lake at all", async () => {
      const { ctx, page } = await open("empty", "/week");
      await settled(page);
      assert.match(await text(page, ".state.empty"), /No weeks in the lake yet/);
      await shoot(page, "health-empty-lake");
      await ctx.close();
    });

    await check("slow lake: the cup and the slow note, then the data (not the timeout)", async () => {
      const { ctx, page } = await open("slow", `/week?week=${w3}`, { width: 390, height: 844 });
      await page.waitForSelector("[data-slow-note]:not([hidden])", { timeout: 5000 });
      assert.equal(await page.getAttribute("section[data-part]", "aria-busy"), "true");
      for (const [w, tag] of [[390, "390"], [1280, "desktop"]]) {
        await page.setViewportSize({ width: w, height: 844 });
        for (const scheme of ["light", "dark"]) {
          await page.emulateMedia({ colorScheme: scheme });
          const bad = await axe(page, `slow ${tag} ${scheme}`);
          assert.deepEqual(bad.map((v) => v.id), []);
          await page.screenshot({ path: `${out}/health-slow-${tag}-${scheme}.png`, fullPage: true });
        }
      }
      const t0 = Date.now();
      await settled(page);
      assert.equal(await page.getAttribute("[data-state]", "data-state"), "data");
      assert.match(await text(page, "[data-answer]"), /7 things to look at/);
      console.log(`slow lake: the data after ${Math.round((Date.now() - t0) / 1000)} s more`);
      await ctx.close();
    });

    await check("lake error: the error state, Retry, and nothing else", async () => {
      const { ctx, page } = await open("broken", `/week?week=${w3}`);
      await settled(page);
      assert.match(await text(page, "[data-state=error]"), /Couldn’t read job_manifests, fetches, builds and serving_log: .*HTTP 503/);
      assert.equal(await page.locator("ul.flags.health").count(), 0);
      await shoot(page, "health-error");
      await ctx.close();
    });

    await check("writes on: the stores that keep failing, a page at a time, each with its Test crawl", async () => {
      const { ctx, page } = await open("act", `/week?week=${w3}`);
      await settled(page);
      const keep = page.locator("#keep-failing + .frame");
      assert.match(await text(page, "#keep-failing"), /Failing week after week \(27\)/);
      assert.equal(await keep.locator("tbody tr").count(), 10);
      assert.match(await text(page, "#keep-failing + .frame tbody tr:first-child"), /always-down\.example 4 weeks/);
      const href = await keep.locator("a", { hasText: "Test crawl" }).first().getAttribute("href");
      assert.equal(href, "/jobs?action=crawl-one&connector=generic&roaster=always-down.example#crawl-one");
      await page.locator("#keep-failing").scrollIntoViewIfNeeded();
      await shoot(page, "health-keep-failing");
      await page.click("nav.pager a:has-text('Next 10')");
      await page.waitForURL(/keep=10/);
      await settled(page);
      assert.equal(await page.locator("#keep-failing + .frame tbody tr").count(), 10);
      assert.match(await text(page, "nav.pager"), /Page 2 of 3/);
      await ctx.close();
    });

    await check("a connector that never ran this week: flagged in each pass, and never a minus in the comparison", async () => {
      const { ctx, page } = await open("gone", `/week?week=${w3}`);
      await settled(page);
      const look = await text(page, "ul.flags.health");
      assert.match(look, /No crawl for woocommerce this week\. None of its morning, resume and retry passes started\./);
      assert.doesNotMatch(look, /No resume crawl/);
      const woo = page.locator("table.vs tr", { hasText: "woocommerce" });
      assert.match((await woo.innerText()).replace(/\s+/g, " "), /woocommerce didn't run No crawl attempt this week\./);
      assert.equal(await woo.getAttribute("class"), "bad");
      await shoot(page, "health-connector-gone");
      await ctx.close();
    });

    await check("no script errors", async () => assert.deepEqual(errors, []));
    await check("axe: no violations, and no sideways scroll at 390 px", async () =>
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
