# Runbook

Step-by-step operations for registry.coffee and the weekly pipeline, including the gotchas we hit the first time.

| Page | When you need it |
|---|---|
| [domain-and-dns.md](domain-and-dns.md) | Setting up or changing DNS for registry.coffee at Namecheap; renewing or moving the domain |
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
registry.coffee (Namecheap BasicDNS) ──A / CNAME──▶ GitHub Pages ◀──deploy── weekly.yml (Mondays)
                                                         │                       │
                                         /  launch page  │                       ├── reads/writes the lake (private HF dataset)
                                         /app/ catalog   │                       └── exports the public HF dataset
```

Everything is on free tiers except the domain ([cost-and-security.md](cost-and-security.md)).

The launch page (`/`) has this week's numbers, new arrivals and links to the catalog app (`/app/`), the public Hugging
Face dataset, the warehouse setup, the MCP server and the "Add a roaster" form. The dataset link and the DuckDB
example come from `PUBLIC_EXPORT_URL` when it's an `hf://` dataset; otherwise the page links to this repo. The
private lake is never linked. `SITE_URL` sets the canonical URL (default `https://registry.coffee`).
