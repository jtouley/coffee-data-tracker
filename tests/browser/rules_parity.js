// The join page's own rule checker (app.js, fed by the page's rules JSON) over the cases in a file; prints its
// verdicts as JSON for tests/test_signup_browser.py to compare with app/rules.py.
// Usage: node rules_parity.js <app url> <cases.json>
const { chromium } = require("playwright");
const fs = require("node:fs");

const [base, file] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(file, "utf8"));
(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.goto(`${base}/join`);
    const verdicts = await page.evaluate(({ usernames, passwords }) => ({
      usernames: usernames.map((u) => checkUsername(u)),
      passwords: passwords.map(([p, u]) => checkPassword(p, u)),
    }), cases);
    process.stdout.write(JSON.stringify(verdicts));
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
