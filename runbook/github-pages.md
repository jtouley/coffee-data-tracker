# GitHub Pages and the custom domain

The weekly workflow builds the site and deploys it with `actions/deploy-pages`; there is no `gh-pages` branch and no
`CNAME` file in the repo. The custom domain is a repository setting.

## One-time setup

1. **Verify the domain on your account** (stops another GitHub account from serving a site on it if Pages is ever
   switched off). Profile picture → **Settings** → **Pages** (under "Code, planning, and automation") →
   **Add a domain** → `registry.coffee`. GitHub shows a TXT record:
   - Host: `_github-pages-challenge-jtouley` (just that; Namecheap appends `.registry.coffee`)
   - Value: the code GitHub shows

   Add it in Namecheap → Advanced DNS, wait a few minutes, click **Verify**. Leave the record in place afterwards;
   GitHub re-checks it.
2. **Repository** → **Settings** → **Pages**:
   - **Source:** GitHub Actions
   - **Custom domain:** `registry.coffee` → **Save**. The DNS check should go green once the A records resolve.
   - **Enforce HTTPS:** tick it once it becomes clickable. Issuing the certificate takes a few minutes to an hour.
3. The `github-pages` environment (Settings → Environments) only allows deploys from `main` by default. Leave it
   that way: a deploy from another branch fails, which is intended.

## What gets deployed

| Path | Built from |
|---|---|
| `/` | `src/coffeedata/templates/landing.html` |
| `/catalog/` | `src/coffeedata/templates/index.html` |
| `/catalog/catalog.json` | `dim_coffee` (listed coffees) |
| `/privacy/`, `/terms/` | `templates/privacy.html`, `templates/terms.html` (layout `_legal.html`); bump `LEGAL_UPDATED` in `site.py` when either changes |

Links on the launch page come from repository variables: `PUBLIC_EXPORT_URL` (an `hf://datasets/...` URL gives the
dataset link and the DuckDB example) and `SITE_URL` (canonical URL, default `https://registry.coffee`).

## Checking it

```bash
curl -sI https://registry.coffee | head -1            # HTTP/2 200
curl -sI http://registry.coffee | grep -i location    # redirects to https once HTTPS is enforced
curl -sI https://www.registry.coffee | grep -i location
curl -s https://registry.coffee | grep -o 'Content-Security-Policy' | head -1
```

## When the smoke test fails

After each Pages deploy, the `smoke` job of `site.yml` (and of `weekly.yml`) runs
`python3 -m coffeedata.live_smoke check site`. It asks the live site for the home, catalog and privacy pages,
`/catalog/catalog.json` and a missing page (a 404). Each answer must have the right status and content (the pages'
titles, Pages' 404 page). The catalog page and download must also match the build just deployed: the catalog page's
data key, the release's build id and one of its coffees. The home and privacy pages look the same from build to
build, so they show the site answers, not which build it serves. Every
request carries a new `?smoke=<run>-<try>` query, so the CDN's ten-minute cache doesn't answer with the old site.
If the build job couldn't record what it built (its "Note what the smoke test expects" step is marked failed), the
`smoke` job fails with "not checked": the built site is likely broken, so treat it as a failure. A red `smoke`
job names the check that failed in the run's summary.

Pages has no traffic to move back, so putting the previous site back means deploying it again:
- **The code broke it** (a template, `site.py`): revert the PR on `main`. Its push runs `site.yml`, which deploys the
  reverted site and smoke-tests it. To restore the site sooner, open the last green `site` run that a push started
  (not a Tuesday scheduled run: re-running one redoes its export and Snowflake load) and use **Re-run all jobs**. It renders the served release with that run's commit and deploys it (the `github-pages` environment allows
  it, because that run was on `main`). The next push to `main` deploys `main` again, so revert as well.
- **The release broke it** (the data): serve the previous release, which redeploys the site from it:
  `gh workflow run weekly -f rollback_to=previous` (docs/ingestion/RUNBOOK.md, §5).
- **The build id doesn't match, and nothing else failed:** another site deploy finished in between (a `site` run and a
  `weekly` run at once), and the site now serves that run's build. Check that run's `smoke` job: if it passed, the
  site is fine and there's nothing to restore.

To check the site by hand, build it (`uv run coffeedata site --served`), then run
`PYTHONPATH=src python3 -m coffeedata.live_smoke check site https://registry.coffee --expect "$(PYTHONPATH=src python3 -m coffeedata.live_smoke expect-site build/site)"`.
