# Cost and security

It runs on free tiers. The only fixed cost is the domain.

| Piece | Cost |
|---|---|
| GitHub Actions and Pages | free for a public repo |
| Hugging Face datasets (lake and public export) | free |
| registry.coffee | $6.98 the first year; .coffee renewals are much higher (about $48 a year at Namecheap), so compare registrars before it renews |
| Claude enrichment (optional) | pay as you go; capped by `--enrich-limit`, and new coffees only after the first run |
| Snowflake, S3 (optional) | only if you turn them on |
| Crawler VM with a fixed IP (proof of concept, optional) | about $3.65 a month for the IP on a free-tier `e2-micro` ([crawler-runner.md](crawler-runner.md)) |

How it's locked down:

- **Workflows.** Actions are pinned to commit SHAs, and Dependabot (`.github/dependabot.yml`) opens a monthly PR to
  update them and the lockfile. The weekly job, which holds the secrets and reads third-party sites, can only read
  the repo; only the deploy job can publish Pages. Checkouts don't keep the git token. The issue-form workflow only
  acts on the owner's issues and passes issue text through files and environment variables, never the shell.
- **The site.** It's static, with no server, cookies or third-party scripts. Each page has a Content-Security-Policy
  that only allows its own inline script (by hash) and same-origin requests. Product links from roasters' feeds are
  kept only if they're plain `http(s)` URLs, and all text is escaped.
- **The crawler runner** (if applied). A self-hosted runner on a public repo: its pre-job hook refuses everything but
  dispatched and scheduled jobs of `live-test.yml` on `main`, and fork PR workflows need approval. See
  [crawler-runner.md](crawler-runner.md#security-model-public-repo).
- **Secrets.** Use a fine-grained Hugging Face token with write access to only the lake and public datasets. Set a
  monthly spend limit on the Anthropic key in the Claude Console. Keep the lake dataset private; only the facts-only
  public export is linked.
- **The domain.** Turn on two-factor authentication at Namecheap and GitHub, keep the domain locked
  (Namecheap → Domain → Registrar Lock) with auto-renew on, and verify the domain in your GitHub profile's Pages
  settings so another account can't serve a site on it. If you ever remove the Pages site, delete its DNS records
  too.
