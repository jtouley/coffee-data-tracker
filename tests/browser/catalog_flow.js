// The catalog's Browse coffees in Chromium (tests/test_catalog_browser.py): search by the roaster's city and state,
// the State filter, and "Show more" past the first 500 rows, over a small catalog.json written by the test. The
// behaviour is checked at 390 px; then each of 320 px, 390 px and desktop, light and dark, gets the no-sideways-scroll
// check (the charts included), legible chart text, a screenshot and axe (where axe-core is installed, as in CI). Also a
// slow catalog.json, and a failed one with Try again. Any request off 127.0.0.1 fails the run.
// Usage: node catalog_flow.js <site url> <screenshot dir> <the page's catalog.json key>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const [site, out, key] = process.argv.slice(2);
let AXE = null;
try { AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"); } catch (e) {}

const loaded = (page) => page.waitForFunction(() => !/^Loading/.test(document.getElementById("count").textContent));

async function open(browser, viewport, scheme, errors, slow = 0, fail = 0) {  // fail: how many requests fail first
  const context = await browser.newContext({ viewport, colorScheme: scheme });
  const fetched = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1") { errors.push(`outside request: ${url}`); return route.abort(); }
    if (url.pathname.endsWith("/catalog.json")) {
      fetched.push(url.search);
      if (fetched.length <= fail) return route.fulfill({ status: 500, body: "oops" });
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
    // A failed one says what to do, and Try again does it: the button keeps focus while it loads (the count says
    // "Loading…"), then hides and focus moves to the search box. Twice failed, it says so again.
    {
      const { context, page, fetched } = await open(browser, { width: 390, height: 844 }, "light", errors, 1000, 2);
      const failed = "Couldn’t load the catalog. Check your connection, then try again.";
      await loaded(page);
      assert.equal(await page.textContent("#count"), failed);
      const again = page.locator("#retry");
      assert.ok(await again.isVisible());
      assert.equal(await again.textContent(), "Try again");
      assert.ok((await again.boundingBox()).height >= 44);
      await again.focus();
      await page.keyboard.press("Enter");
      await page.waitForFunction((t) => document.getElementById("count").textContent === t, failed);
      assert.ok(await again.isVisible() && (await again.getAttribute("aria-disabled")) === null);
      await page.keyboard.press("Enter");
      assert.equal(await page.textContent("#count"), "Loading…");
      assert.equal(await again.getAttribute("aria-disabled"), "true");
      await loaded(page);
      assert.match(await page.textContent("#count"), /^\d[\d,]* coffees? from/);
      assert.ok(await again.isHidden());
      assert.equal(await page.evaluate(() => document.activeElement.id), "q");
      assert.equal(fetched.length, 3);
      await context.close();
    }

    const { context, page } = await open(browser, { width: 390, height: 844 }, "light", errors);
    await loaded(page);
    // On a phone the filters fold behind a "Filters" button (a disclosure), with Sort beside it.
    const fbtn = page.locator("#fbtn");
    assert.equal(await fbtn.getAttribute("aria-expanded"), "false");
    assert.ok(await fbtn.isVisible() && (await page.isHidden("#origin")) && (await page.isVisible("#sort")));
    assert.ok((await fbtn.boundingBox()).height >= 44);
    const sameRow = async () => Math.abs((await fbtn.boundingBox()).y - (await page.locator("#sort").boundingBox()).y) < 4;
    assert.ok(await sameRow());
    await fbtn.click();
    assert.equal(await fbtn.getAttribute("aria-expanded"), "true");
    assert.ok((await page.isVisible("#origin")) && (await sameRow()));  // Sort stays beside it, open or closed
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
    // On a phone the roaster column drops and the roaster and place sit under the coffee: two lines a row.
    assert.equal(await page.textContent("#rows tr:first-child .by"), "Mitten Coffee CoDetroit, MI");
    assert.ok(!(await page.isVisible("#rows tr:first-child td:nth-child(3)")));
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

    // Sort: Roaster A–Z (the file's order), or by $/100 g either way; coffees with no price come last in both, in
    // roaster order, and the line says how many. The header carries aria-sort and an arrow.
    const priceTh = page.locator("#price");
    assert.equal(await search("blend"), "5 coffees matching “blend” from 5 roasters.");
    assert.deepEqual(await titles(), ["Dry Creek Blend", "Empire Blend", "Gotham Blend", "Lakefront Blend", "Hill Blend"]);
    assert.equal(await priceTh.getAttribute("aria-sort"), null);
    await page.selectOption("#sort", "asc");
    assert.equal(await count(), "5 coffees matching “blend” from 5 roasters. Lowest price per 100 g first. 2 with no price are listed last.");
    assert.deepEqual(await titles(), ["Hill Blend", "Lakefront Blend", "Empire Blend", "Dry Creek Blend", "Gotham Blend"]);
    assert.equal(await priceTh.getAttribute("aria-sort"), "ascending");
    assert.equal(await priceTh.textContent(), "$/100 g ↑");
    await page.selectOption("#sort", "desc");
    assert.equal(await count(), "5 coffees matching “blend” from 5 roasters. Highest price per 100 g first. 2 with no price are listed last.");
    assert.deepEqual(await titles(), ["Empire Blend", "Lakefront Blend", "Hill Blend", "Dry Creek Blend", "Gotham Blend"]);
    assert.equal(await priceTh.getAttribute("aria-sort"), "descending");
    // With the filters: the State filter, and the Filters button names what's on.
    await page.selectOption("#state", "KS");
    assert.equal(await count(), "1 coffee matching “blend” from 1 Kansas roaster. Highest price per 100 g first.");
    assert.equal(await fbtn.textContent(), "Filters · Kansas");
    await page.uncheck("#stock");
    assert.equal(await fbtn.textContent(), "Filters · Kansas, sold out too");
    await page.check("#stock");
    await page.selectOption("#state", "");
    // A new sort starts at the first page again: past 500, Show more, then a change of sort.
    const total = Number((await search("")).match(/^[\d,]+/)[0].replace(/,/g, ""));
    await page.locator("#more").click();
    assert.ok(total > 500 && (await rowCount()) === total);
    await page.selectOption("#sort", "asc");
    assert.equal(await rowCount(), 500);
    assert.equal(await page.locator("#more").textContent(), `Show ${total - 500} more (500 of ${total} shown)`);
    assert.equal(await page.textContent("#rows tr:first-child td:first-child a"), "Hill Blend");  // the cheapest
    await page.selectOption("#sort", "roaster");
    assert.equal(await priceTh.textContent(), "$/100 g");

    // Keyboard: the controls in order, each with a name.
    await page.focus("#q");
    const order = [];
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Tab");
      order.push(await page.evaluate(() => { const el = document.activeElement, label = el.labels?.[0];
        return [el.id, el.getAttribute("aria-label") || (label ? [...label.childNodes].filter((c) => c.nodeType === 3).map((c) => c.textContent).join("").trim() : el.textContent.trim())]; }));
    }
    assert.deepEqual(order, [["fbtn", "Filters"], ["sort", "Sort"], ["origin", "Origin"], ["process", "Process"], ["state", "Roaster state (number of roasters)"], ["stock", "In stock"]]);
    await context.close();

    // Each width and theme: the roaster's column on desktop, no sideways scroll (the charts' grid scrolled 16 px at
    // 320 px), chart text at 12 px or more, axe, a screenshot.
    for (const [tag, viewport] of [["320", { width: 320, height: 720 }], ["390", { width: 390, height: 844 }], ["desktop", { width: 1280, height: 800 }],
      ["zoom200", { width: 640, height: 400 }]]) {  // a 1280 x 800 window at 200% zoom
      for (const scheme of ["light", "dark"]) {
        const { context, page } = await open(browser, viewport, scheme, errors);
        await loaded(page);
        await page.fill("#q", "Michigan");
        await page.waitForTimeout(400);
        if (tag === "desktop") {
          assert.ok(await page.isVisible("#rows tr:first-child td:nth-child(2) .place"));
          assert.ok(!(await page.isVisible("#rows tr:first-child .by")));
          // Wide enough: no Filters button, the filters show.
          assert.ok((await page.isHidden("#fbtn")) && (await page.isVisible("#origin")));
        }
        // The search field and the table stay inside the section (the field ran 3 px past it at 320 px).
        const inside = await page.evaluate(() => {
          const sec = document.getElementById("q").closest("section").getBoundingClientRect(), pad = 20;
          const box = document.querySelector(".scroll");
          return document.getElementById("q").getBoundingClientRect().right <= sec.right - pad + 0.5 && box.scrollWidth <= box.clientWidth;
        });
        assert.ok(inside, `${tag}: the search field or the table runs past the section`);
        // The charts' marks take no keyboard focus: their numbers are in each chart's table.
        assert.equal(await page.$$eval(".chart .mark[tabindex]", (m) => m.length), 0);
        const wide = await page.evaluate(() => document.documentElement.scrollWidth);
        assert.ok(wide <= viewport.width, `the catalog scrolls sideways at ${tag} (${wide} px wide)`);
        // The text of each chart that shows, in screen pixels: a viewBox scales it with the chart's width.
        const sizes = await page.$$eval(".chart", (cs) => cs.filter((c) => c.getBoundingClientRect().width).map((c) => {
          const vb = c.viewBox.baseVal, k = vb && vb.width ? c.getBoundingClientRect().width / vb.width : 1;
          return parseFloat(getComputedStyle(c.querySelector("text")).fontSize) * k; }));
        assert.equal(sizes.length, 2, `${tag}: one drawing of each chart shows`);
        assert.ok(sizes.every((px) => px >= 11.5), `${tag}: chart text at ${sizes.map((px) => px.toFixed(1))} px`);
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
