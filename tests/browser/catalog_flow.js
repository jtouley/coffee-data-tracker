// The catalog's Browse coffees in Chromium (tests/test_catalog_browser.py): search by the roaster's city and state,
// the State filter, and "Show more" past the first 500 rows, over a small catalog.json written by the test. The
// behaviour is checked at 390 px; then each of 390 px and desktop, light and dark, gets the no-sideways-scroll check,
// a screenshot and axe (where axe-core is installed, as in CI). Also a slow catalog.json and a failed one. Any
// request off 127.0.0.1 fails the run.
// Usage: node catalog_flow.js <site url> <screenshot dir> <the page's catalog.json key>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [site, out, key] = process.argv.slice(2);
let AXE = null;
try { AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"); } catch (e) {}

const loaded = (page) => page.waitForFunction(() => !/^Loading/.test(document.getElementById("count").textContent));

async function open(browser, viewport, scheme, errors, slow = 0, fail = false) {
  const context = await browser.newContext({ viewport, colorScheme: scheme });
  const fetched = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1") { errors.push(`outside request: ${url}`); return route.abort(); }
    if (url.pathname.endsWith("/catalog.json")) {
      fetched.push(url.search);
      if (fail) return route.fulfill({ status: 500, body: "oops" });
      if (slow) await new Promise((r) => setTimeout(r, slow));
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
  await page.goto(`${site}/catalog/`);
  return { context, page, fetched };
}

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  try {
    // A slow catalog.json: "Loading…" until it arrives; the page asks for its own file (?b=<a hash of it>).
    {
      const { context, page, fetched } = await open(browser, { width: 390, height: 844 }, "light", errors, 1500);
      assert.equal(await page.textContent("#count"), "Loading…");
      await loaded(page);
      assert.match(await page.textContent("#count"), /^\d[\d,]* coffees? from/);
      assert.deepEqual(fetched, [`?b=${key}`]);
      await context.close();
    }
    // A failed one says so.
    {
      const { context, page } = await open(browser, { width: 390, height: 844 }, "light", errors, 0, true);
      await loaded(page);
      assert.equal(await page.textContent("#count"), "Couldn't load the catalog.");
      await context.close();
    }

    const { context, page } = await open(browser, { width: 390, height: 844 }, "light", errors);
    await loaded(page);
    // Typing waits for a pause (longer for a lone two-letter code), then the table and the count change together.
    const search = async (q) => { await page.fill("#q", q); await page.waitForTimeout(900); return page.textContent("#count"); };
    const count = async () => { await page.waitForTimeout(100); return page.textContent("#count"); };
    const titles = () => page.$$eval("#rows tr td:first-child", (tds) =>
      [...new Set(tds.map((td) => td.querySelector("a")?.textContent ?? td.firstChild.textContent))]);
    const roasters = async () => [...new Set(await page.$$eval("#rows tr td:nth-child(2)", (tds) => tds.map((td) => td.firstChild.textContent)))];
    const rowCount = () => page.$$eval("#rows tr", (t) => t.length);

    // The result count is a live region, so screen readers hear each new count.
    assert.equal(await page.getAttribute("#count", "role"), "status");
    // The place: "City, ST" after the roaster, as stored; the state alone where the city is missing.
    await search("dry creek");
    assert.deepEqual(await page.$$eval("#rows tr td:nth-child(2) .place", (ps) => ps.map((p) => p.textContent)), ["TX"]);
    await search("detroit");
    assert.equal(await page.textContent("#rows tr:first-child td:nth-child(2) .place"), "Detroit, MI");
    // On a phone the roaster column drops and the roaster and place sit under the coffee.
    assert.equal(await page.textContent("#rows tr:first-child .by"), "Mitten Coffee CoDetroit, MI");
    assert.ok(await page.isVisible("#rows tr:first-child .by") && !(await page.isVisible("#rows tr:first-child td:nth-child(2)")));

    // A lone two-letter code is the state and nothing else, in any case: not Miami, not notes with "mi" in them.
    for (const q of ["mi", "MI", "Mi"]) {
      assert.equal(await search(q), "620 coffees from roasters in Michigan (MI).", q);
      assert.deepEqual(await roasters(), ["Mitten Coffee Co"], q);
    }
    // ...but not while it's the start of a longer word: typing "mild" never shows Michigan's coffees on the way.
    await search("");
    await page.evaluate(() => { window.seen = []; new MutationObserver(() => window.seen.push(document.getElementById("count").textContent))
      .observe(document.getElementById("count"), { childList: true, characterData: true, subtree: true }); });
    // Nor with a capital first letter, as phones type it: "Mild", "Colombia", "Panama", "Washed" and "Decaf" never
    // show a state (MI, CO, PA, WA, DE) on the way, nor a result for the two-letter start alone.
    for (const q of ["mild", "Mild", "Colombia", "Panama", "Washed", "Decaf"]) {
      await page.fill("#q", ""); await page.waitForTimeout(250);
      await page.evaluate(() => { window.seen = []; });
      await page.type("#q", q, { delay: 250 });
      await page.waitForTimeout(400);
      const seen = await page.evaluate(() => window.seen);
      const flash = (t) => /Michigan|Colorado|Pennsylvania|Washington|Delaware/.test(t) || t.includes(`“${q.slice(0, 2)}”`);
      assert.ok(seen.length && !seen.some(flash), `${q}: ${seen.join(" | ")}`);
    }
    // The count and the table change together.
    await search("mild");
    assert.equal(await rowCount(), Math.min(500, Number((await page.textContent("#count")).match(/^[\d,]+/)[0].replace(/,/g, ""))));
    await search("Decaf");
    assert.equal(await rowCount(), 0);
    assert.equal(await page.textContent("#count"), "No coffees in stock match “Decaf”.");
    // "in" is Indiana: not Indonesia, and not the word "in" in a note.
    assert.equal(await search("IN"), "1 coffee from roasters in Indiana (IN).");
    assert.deepEqual(await titles(), ["Lakefront Blend"]);
    assert.equal(await search("in"), "1 coffee from roasters in Indiana (IN).");
    // Words other than codes still match inside the text, as before.
    await search("indonesia");
    assert.deepEqual(await titles(), ["Indonesia Sumatra"]);
    // A state's name: its roasters' coffees, plus coffees that name it, such as a roaster in Michigan City, IN.
    assert.equal(
      await search("Michigan"),
      "622 coffees: 620 from roasters in Michigan, and 2 that mention “Michigan” in their name, roaster, city or notes."
    );
    assert.deepEqual((await roasters()).sort(), ["Little Havana Roasters", "Maple City Coffee", "Mitten Coffee Co"]);
    // A city is read first, as a phrase, wherever it is: "Michigan City" is Michigan City, IN, never Michigan's
    // roasters (whose "Mitten City Roast" has "city" and "coffee" in it).
    for (const q of ["Michigan City", "michigan city coffee", "michigan city, in"]) {
      assert.match(await search(q), /^1 coffee (matching “coffee” )?from roasters in Michigan City, IN\.$/, q);
      assert.deepEqual(await titles(), ["Lakefront Blend"], q);
    }
    assert.equal(await search("kansas city roasters"),
      "2 coffees matching “roasters” from roasters in Kansas City, MO and Kansas City, KS.");
    // A name is whole words: Kansas isn't Arkansas. Kansas City, MO mentions Kansas.
    assert.equal(await search("kansas"),
      "2 coffees: 1 from roasters in Kansas, and 1 that mentions “Kansas” in its name, roaster, city or notes.");
    assert.deepEqual(await titles(), ["Plaza Espresso", "Hill Blend"]);
    await search("arkansas");
    assert.deepEqual(await titles(), ["Ozark Morning"]);
    // The result line names the city when the search does.
    for (const q of ["Chicago", "chicago, il", "Illinois chicago"]) {
      assert.equal(await search(q), "1 coffee from roasters in Chicago, IL.", q);
      assert.deepEqual(await titles(), ["Windy City Espresso"], q);
    }
    assert.equal(await search("ILLINOIS"), "1 coffee from roasters in Illinois.");
    assert.equal(await search("new york"), "2 coffees from roasters in New York.");
    // "New York City" and "NYC" are the city; "new york" stays the state.
    for (const q of ["new york city", "NYC", "nyc gotham"]) {
      assert.match(await search(q), /^1 coffee (matching “gotham” )?from roasters in New York, NY\.$/, q);
      assert.deepEqual(await titles(), ["Gotham Blend"], q);
    }
    assert.equal(await search("vermont"), "1 coffee mentions “Vermont” in its name, roaster, city or notes.");
    // Beside other words a code may also be that whole word, so a roaster named "La ..." is still found.
    assert.equal(await search("la paloma"), "1 coffee matching “paloma” from 1 roaster.");
    assert.deepEqual(await roasters(), ["La Paloma Coffee"]);
    // No results: what was searched, and which filters were on.
    assert.equal(await search("zzqx"), "No coffees in stock match “zzqx”.");
    await page.uncheck("#stock");
    assert.equal(await count(), "No coffees match “zzqx”.");
    await page.check("#stock");
    await page.selectOption("#process", "natural");
    assert.equal(await count(), "No coffees match “zzqx” with these filters (in stock only).");
    await page.selectOption("#process", "");

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
    assert.equal(await search("cherry"), "20 coffees matching “cherry” from 1 Michigan roaster.");
    await page.selectOption("#process", "natural");
    assert.equal(await count(), "10 coffees matching “cherry” from 1 Michigan roaster.");
    await page.selectOption("#process", "");
    // Not the IN or FL ones; the line quotes the search too.
    assert.equal(await search("Michigan"), "620 coffees matching “Michigan” from 1 Michigan roaster.");
    await page.uncheck("#stock");
    assert.match(await count(), /^621 coffees/);  // the sold-out one
    await page.check("#stock");

    // Past 500: the first page, then "Show more" draws the rest and moves focus to the first new row.
    await search("");
    assert.equal(await rowCount(), 500);
    const more = page.locator("#more");
    assert.equal(await more.textContent(), "Show 120 more (500 of 620 shown)");
    assert.ok((await more.boundingBox()).height >= 48);
    await more.focus();
    await page.keyboard.press("Enter");
    assert.equal(await rowCount(), 620);
    assert.ok(await more.isHidden());
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "Mitten 501");
    // A new search starts at the first page again.
    await page.selectOption("#state", "");
    await search("mitten");
    assert.equal(await rowCount(), 500);

    // Keyboard: the filters in order, each with a name.
    await page.focus("#q");
    const order = [];
    for (let i = 0; i < 4; i++) {
      await page.keyboard.press("Tab");
      order.push(await page.evaluate(() => [document.activeElement.id, document.activeElement.getAttribute("aria-label") || document.activeElement.labels?.[0]?.textContent.trim()]));
    }
    assert.deepEqual(order, [["origin", "Origin"], ["process", "Process"], ["state", "Roaster state (number of roasters)"], ["stock", "In stock"]]);
    await context.close();

    // Each width and theme: the roaster's column on desktop, no sideways scroll, axe, a screenshot.
    for (const [tag, viewport] of [["390", { width: 390, height: 844 }], ["desktop", { width: 1280, height: 800 }]]) {
      for (const scheme of ["light", "dark"]) {
        const { context, page } = await open(browser, viewport, scheme, errors);
        await loaded(page);
        await page.fill("#q", "Michigan");
        await page.waitForTimeout(400);
        if (tag === "desktop") {
          assert.ok(await page.isVisible("#rows tr:first-child td:nth-child(2) .place"));
          assert.ok(!(await page.isVisible("#rows tr:first-child .by")));
        }
        const wide = await page.evaluate(() => document.documentElement.scrollWidth);
        assert.ok(wide <= viewport.width, `the catalog scrolls sideways at ${tag} (${wide} px wide)`);
        if (AXE) {
          await page.evaluate(AXE);  // over the DevTools protocol, which the page's CSP doesn't apply to
          const r = await page.evaluate(async () => (await window.axe.run(document, { resultTypes: ["violations"] })).violations
            .filter((v) => ["serious", "critical"].includes(v.impact)).map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(" ")}`));
          assert.deepEqual(r, [], `axe (${tag} ${scheme})`);
        }
        await page.locator("section", { has: page.locator("#q") }).screenshot({ path: `${out}/catalog-michigan-${tag}-${scheme}.png` });
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }
  assert.deepEqual(errors, []);
})().catch((e) => { console.error(e); process.exit(1); });
