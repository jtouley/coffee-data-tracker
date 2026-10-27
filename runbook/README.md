# Runbook

Step-by-step operations for registry.coffee and the weekly pipeline, including the gotchas we hit the first time.

| Page | When you need it |
|---|---|
| [domain-and-dns.md](domain-and-dns.md) | Setting up or changing DNS for registry.coffee at Namecheap; renewing or moving the domain |
| [github-pages.md](github-pages.md) | Connecting the domain to GitHub Pages, HTTPS, domain verification |
| [first-run.md](first-run.md) | Secrets and variables, the first deploy, and re-running the weekly job |
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

Everything is on free tiers except the domain (see "Cost and security" in the main README).
