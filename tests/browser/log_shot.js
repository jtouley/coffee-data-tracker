// Log shot -> Saved -> Undo in Chromium at 390 px, against a running app. Run by tests/test_app_browser.py.
// Usage: node log_shot.js <base url> <screenshot dir>
const { chromium } = require("playwright");
const assert = require("node:assert/strict");

const [base, out] = process.argv.slice(2);

async function shoot(page, name) {
  await page.evaluate(() => { document.activeElement?.blur(); scrollTo(0, 0); });
  await page.waitForTimeout(300); // let the 120 ms selection fades finish
  // The whole screen at 390 px wide, so nothing sits under the sticky Save button.
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: 390, height: Math.max(844, height) });
  await page.screenshot({ path: `${out}/${name}.png` });
  await page.setViewportSize({ width: 390, height: 844 });
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  try {
    await page.goto(`${base}/`);
    await shoot(page, "today");
    await page.getByRole("link", { name: /Pull again/ }).click();
    await page.waitForURL(`${base}/log`);

    // Prefilled from the last shot; time, taste and rating start empty.
    assert.equal(await page.inputValue("#dose_g"), "18.0");
    assert.equal(await page.inputValue("#yield_g"), "36.4");
    assert.equal(await page.inputValue("#grinder_setting"), "11");
    assert.equal(await page.locator('[aria-pressed="true"]').count(), 0);
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

    await page.getByRole("button", { name: "Save shot" }).click();
    await page.waitForURL(/\/saved\/.+\?new=1$/);
    const saved = await page.textContent("main");
    assert.match(saved, /1:2\.03/);
    assert.match(saved, /Inside the window, one second faster than \w+day’s shot\./);
    assert.match(saved, /Balanced · 5/);
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
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
