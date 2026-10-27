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

Links on the launch page come from repository variables: `PUBLIC_EXPORT_URL` (an `hf://datasets/...` URL gives the
dataset link and the DuckDB example) and `SITE_URL` (canonical URL, default `https://registry.coffee`).

## Checking it

```bash
curl -sI https://registry.coffee | head -1            # HTTP/2 200
curl -sI http://registry.coffee | grep -i location    # redirects to https once HTTPS is enforced
curl -sI https://www.registry.coffee | grep -i location
curl -s https://registry.coffee | grep -o 'Content-Security-Policy' | head -1
```
