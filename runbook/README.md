# Runbook

Step-by-step operations for registry.coffee and the weekly pipeline, including the gotchas we hit the first time.

| Page | When you need it |
|---|---|
| [domain-and-dns.md](domain-and-dns.md) | DNS for registry.coffee at Namecheap, app.registry.coffee on Cloud Run, renewing or moving the domain |
| [github-pages.md](github-pages.md) | Connecting the domain to GitHub Pages, HTTPS, domain verification |
| [first-run.md](first-run.md) | Secrets and variables, the first deploy, and re-running the weekly job |
| [cost-and-security.md](cost-and-security.md) | What it costs to run, and how the workflows, site, secrets and domain are locked down |
| [troubleshooting.md](troubleshooting.md) | The site is down, the DNS check fails, a run failed |

`check-dns.sh` prints every record the site depends on, as the public resolvers see it:

```bash
runbook/check-dns.sh                 # registry.coffee
runbook/check-dns.sh example.coffee  # another domain
```

## How it fits together

```
Cloud Run jobs (Cloud Scheduler, UTC) ── crawl Mondays, build Tuesdays 06:17 ──▶ the lake (private HF dataset)
                                                                                     │ served release (read only)
registry.coffee (Namecheap BasicDNS) ──A / CNAME──▶ GitHub Pages ◀──deploy── site.yml (Tuesdays 11:17)
                                                         │                       └── exports the public HF dataset
                                    / launch page, /catalog/ catalog
```

`weekly.yml` has no schedule: it is the manual fallback, run by hand to crawl, build and deploy, or to roll back.
The Cloud Run pipeline is in [`docs/ingestion/RUNBOOK.md`](../docs/ingestion/RUNBOOK.md).

GitHub and Hugging Face are free here; the Cloud Run jobs, NAT and the domain are not
([cost-and-security.md](cost-and-security.md)).

The launch page (`/`) has this week's numbers, new arrivals and links to the catalog (`/catalog/`), the public Hugging
Face dataset, the warehouse setup, the MCP server and the "Add a roaster" form. The dataset link and the DuckDB
example come from `PUBLIC_EXPORT_URL` when it's an `hf://` dataset; otherwise the page links to this repo. The
private lake is never linked. `SITE_URL` sets the canonical URL (default `https://registry.coffee`).
