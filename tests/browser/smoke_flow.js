// The smoke suite's browser pass (tests/test_smoke_web.py), in Chromium at 390 px: the static site's landing page
// and catalog, then the app's join page and a seeded user's sign-in. Any request off 127.0.0.1 (but the usage
// counter's, answered here) is aborted and fails the run, as does a script error or a CSP refusal.
// Usage: node smoke_flow.js <site url> <app url> <screenshot dir> <username> <password>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");

const [site, app, out, username, password] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await context.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host === "127.0.0.1" || route.request().url().startsWith("data:")) return route.continue();
      // The usage counter (coffeedata/beacon.py) is answered here, as it would be: never reached from a test.
      if (route.request().url() === "https://e.registry.coffee/e") return route.fulfill({ status: 204 });
      errors.push(`outside request: ${route.request().url()}`);
      return route.abort();
    });
    const page = await context.newPage();
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
    const noScroll = async (name) => {
      const wide = await page.evaluate(() => document.documentElement.scrollWidth);
      assert.ok(wide <= 390, `${name} scrolls sideways at 390 px (${wide} px wide)`);
      await page.screenshot({ path: `${out}/${name}.png`, fullPage: true });
    };

    // The landing page: its counts are the build's, and none is zero.
    await page.goto(`${site}/`);
    const stats = await page.$$eval(".stat .n", (ns) => ns.map((n) => Number(n.textContent.replace(/,/g, ""))));
    assert.ok(stats.length >= 4 && stats.every((n) => n > 0), `landing counts: ${stats}`);
    await noScroll("landing");

    // The catalog: its script loads catalog.json and fills the table.
    await page.goto(`${site}/catalog/`);
    // Wait for the load to finish, whatever it says ("1 coffee", "No coffees…", or an error), then check it.
    await page.waitForFunction(() => !/^Loading/.test(document.getElementById("count").textContent));
    const line = await page.textContent("#count");
    const shown = Number((line.match(/^([\d,]+) coffees? /) || [, "0"])[1].replace(/,/g, ""));
    assert.ok(shown > 0 && (await page.$$("#rows tr")).length === Math.min(shown, 500), `catalog says: ${line}`);
    await noScroll("catalog");

    // The app: the join page loads with its form; the seeded user signs in and lands on Today.
    await page.goto(`${app}/join`);
    assert.equal(await page.textContent("h1"), "You’re invited");
    assert.ok(await page.isVisible("#join #code"));
    await noScroll("join");
    await page.goto(`${app}/`);
    assert.equal(new URL(page.url()).pathname, "/signin");
    await page.fill("#username", username);
    await page.fill("#password", password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(`${app}/`);
    assert.match(await page.textContent("main"), /Nothing on the grinder yet/);
    await noScroll("signed-in");
  } finally {
    await browser.close();
  }
  assert.deepEqual(errors, []);
})().catch((e) => { console.error(e); process.exit(1); });
