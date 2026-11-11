// The catalog's Browse coffees in Chromium (tests/test_catalog_browser.py): search by the roaster's city and state,
// the State filter, and "Show more" past the first 500 rows, over a small catalog.json written by the test. At 375 px,
// in light and dark; axe runs too where axe-core is installed (CI). Any request off 127.0.0.1 fails the run.
// Usage: node catalog_flow.js <site url> <screenshot dir>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [site, out] = process.argv.slice(2);
let AXE = null;
try { AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"); } catch (e) {}

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  try {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width: 375, height: 812 }, colorScheme: scheme });
      await context.route("**/*", (route) => {
        if (new URL(route.request().url()).hostname === "127.0.0.1") return route.continue();
        errors.push(`outside request: ${route.request().url()}`);
        return route.abort();
      });
      const page = await context.newPage();
      page.on("pageerror", (e) => errors.push(e.message));
      page.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
      await page.goto(`${site}/catalog/`);
      await page.waitForFunction(() => /^\d[\d,]* coffees/.test(document.getElementById("count").textContent));

      // Wait for the result line after a change (typing updates it once typing pauses).
      const count = async () => { await page.waitForTimeout(450); return page.textContent("#count"); };
      const titles = () => page.$$eval("#rows tr td:first-child", (tds) =>
        [...new Set(tds.map((td) => td.querySelector("a")?.textContent ?? td.firstChild.textContent))]);
      const search = async (q) => { await page.fill("#q", q); return count(); };
      const roasters = async () => [...new Set(await page.$$eval("#rows tr td:nth-child(2)", (tds) => tds.map((td) => td.firstChild.textContent)))];

      if (scheme === "light") {
        // The result count is a live region, so screen readers hear each new count.
        assert.equal(await page.getAttribute("#count", "role"), "status");
        // The place line: "City, ST" under the roaster, as stored; the state alone where the city is missing.
        await search("dry creek");
        assert.deepEqual(await page.$$eval("#rows tr td:nth-child(2) .place", (ps) => ps.map((p) => p.textContent)), ["TX"]);
        await search("detroit");
        assert.equal(await page.textContent("#rows tr:first-child td:nth-child(2) .place"), "Detroit, MI");
        // On a phone the roaster column drops and the roaster and place sit under the coffee.
        assert.equal(await page.textContent("#rows tr:first-child .by"), "Mitten Coffee CoDetroit, MI");
        assert.ok(await page.isVisible("#rows tr:first-child .by") && !(await page.isVisible("#rows tr:first-child td:nth-child(2)")));

        // A lone two-letter code is the state and nothing else, in any case: not Miami, not notes with "mi" in them.
        for (const q of ["mi", "MI", "Mi"]) {
          assert.match(await search(q), /^620 coffees from roasters in Michigan \(MI\)\.$/, q);
          assert.deepEqual(await roasters(), ["Mitten Coffee Co"], q);
        }
        // "in" is Indiana: not Indonesia, and not the word "in" in a note.
        assert.match(await search("IN"), /^1 coffee from roasters in Indiana \(IN\)\.$/);
        assert.deepEqual(await titles(), ["Lakefront Blend"]);
        assert.equal(await search("in"), await page.textContent("#count"));
        assert.deepEqual(await titles(), ["Lakefront Blend"]);
        // Words other than codes still match inside the text, as before.
        await search("indonesia");
        assert.deepEqual(await titles(), ["Indonesia Sumatra"]);
        // A state's name: its roasters' coffees, plus coffees that name it, such as a roaster in Michigan City, IN.
        assert.equal(
          await search("Michigan"),
          "622 coffees: 620 from roasters in Michigan, and 2 that mention “Michigan” in their name, roaster, city or notes."
        );
        assert.deepEqual((await roasters()).sort(), ["Little Havana Roasters", "Maple City Roasters", "Mitten Coffee Co"]);
        // A city named for a state is the city: "Michigan City" is Michigan City, IN, not every Michigan roaster.
        assert.equal(await search("Michigan City"), "1 coffee from 1 roaster.");
        assert.deepEqual(await titles(), ["Lakefront Blend"]);
        // A state with no roasters here still finds coffees that name it, and says so.
        assert.equal(await search("vermont"), "1 coffee mentions “Vermont” in its name, roaster, city or notes.");
        // A name is whole words: Kansas isn't Arkansas.
        assert.equal(await search("kansas"), "No coffees match “kansas” with these filters.");
        await search("arkansas");
        assert.deepEqual(await titles(), ["Ozark Morning"]);
        // City, "City, ST", a state's name in any case, and two-word names.
        for (const q of ["Chicago", "chicago, il", "ILLINOIS", "Illinois chicago"]) {
          assert.match(await search(q), /^1 coffee/, q);
          assert.deepEqual(await titles(), ["Windy City Espresso"], q);
        }
        assert.match(await search("new york"), /^1 coffee from roasters in New York\.$/);
        // Beside other words a code may also be that whole word, so a roaster named "La ..." is still found.
        assert.equal(await search("la paloma"), "1 coffee from 1 roaster.");  // not "Louisiana"
        assert.deepEqual(await roasters(), ["La Paloma Coffee"]);

        // The State filter: options are the roasters' states by name, with the number of roasters in each that have
        // coffees under the other filters, so the count agrees with the result line.
        await search("chicago");
        const counts = await page.$$eval("#state option", (os) => os.map((o) => o.textContent));
        assert.ok(counts.includes("Illinois (1)") && counts.includes("Michigan (0)"), counts.join());
        await search("");
        const options = await page.$$eval("#state option", (os) => os.map((o) => o.textContent));
        assert.deepEqual(options.slice(0, 3), ["All states", "Arkansas (1)", "California (1)"]);
        assert.ok(options.includes("Michigan (1)") && options.includes("District of Columbia (1)"));
        await page.selectOption("#state", "IL");
        assert.equal(await count(), "1 coffee from 1 Illinois roaster.");
        // It combines with the search box and the other filters.
        await page.selectOption("#state", "MI");
        assert.match(await search("cherry"), /^20 coffees from 1 Michigan roaster\.$/);
        await page.selectOption("#process", "natural");
        assert.match(await count(), /^10 coffees from 1 Michigan roaster\.$/);
        await page.selectOption("#process", "");
        await search("Michigan");
        assert.match(await page.textContent("#count"), /^620 coffees from 1 Michigan roaster\.$/);  // not the IN or FL ones
        await page.uncheck("#stock");
        assert.match(await count(), /^621 coffees/);  // the sold-out one
        await page.check("#stock");

        // Past 500: the first page, then "Show more" draws the rest and moves focus to the first new row.
        await search("");
        assert.equal(await page.$$eval("#rows tr", (t) => t.length), 500);
        const more = page.locator("#more");
        assert.equal(await more.textContent(), "Show 120 more (500 of 620 shown)");
        await more.focus();
        await page.keyboard.press("Enter");
        assert.equal(await page.$$eval("#rows tr", (t) => t.length), 620);
        assert.ok(await more.isHidden());
        assert.equal(await page.evaluate(() => document.activeElement.textContent), "Mitten 501");
        // A new search starts at the first page again.
        await page.selectOption("#state", "");
        await search("mitten");
        assert.equal(await page.$$eval("#rows tr", (t) => t.length), 500);

        // Keyboard: the filters in order, each with a name.
        await page.focus("#q");
        const order = [];
        for (let i = 0; i < 4; i++) {
          await page.keyboard.press("Tab");
          order.push(await page.evaluate(() => [document.activeElement.id, document.activeElement.getAttribute("aria-label") || document.activeElement.labels?.[0]?.textContent.trim()]));
        }
        assert.deepEqual(order, [["origin", "Origin"], ["process", "Process"], ["state", "Roaster state (number of roasters)"], ["stock", "In stock"]]);
      }
      await search("Michigan");
      const wide = await page.evaluate(() => document.documentElement.scrollWidth);
      assert.ok(wide <= 375, `the catalog scrolls sideways at 375 px (${wide} px wide)`);
      if (AXE) {
        await page.evaluate(AXE);  // over the DevTools protocol, which the page's CSP doesn't apply to
        const r = await page.evaluate(async () => (await window.axe.run(document, { resultTypes: ["violations"] })).violations
          .filter((v) => ["serious", "critical"].includes(v.impact)).map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(" ")}`));
        assert.deepEqual(r, [], `axe (${scheme})`);
      }
      await page.locator("section", { has: page.locator("#q") }).screenshot({ path: `${out}/catalog-michigan-${scheme}.png` });
      await context.close();
    }
  } finally {
    await browser.close();
  }
  assert.deepEqual(errors, []);
})().catch((e) => { console.error(e); process.exit(1); });
