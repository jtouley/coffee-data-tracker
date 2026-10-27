# First deploy and the weekly run

## Repository settings

Settings → Secrets and variables → Actions.

| Name | Kind | Required | Value |
|---|---|---|---|
| `LAKE_URL` | variable | yes | `hf://datasets/jtouley/coffee-data` (the private lake) |
| `HF_TOKEN` | secret | yes | a **fine-grained** Hugging Face token with write access to only the lake and public datasets |
| `PUBLIC_EXPORT_URL` | variable | for the dataset link | e.g. `hf://datasets/jtouley/us-coffee-market` (public) |
| `SITE_URL` | variable | no | defaults to `https://registry.coffee` |
| `ANTHROPIC_API_KEY` | secret | no | enables Claude enrichment; set a monthly spend limit in the Claude Console |
| `SNOWFLAKE_*`, `AWS_*` | | no | see `marketplace/README.md` |

Fine-grained HF token: huggingface.co → Settings → Access Tokens → **Create new token** → *Fine-grained* →
"Repositories permissions" → add both datasets with **Write**. Nothing else.

Issue labels `shot` and `roaster` must exist for the issue forms.

## First deploy

1. Merge to `main` (Pages deploys only from `main`).
2. Actions → **weekly** → **Run workflow** → `sample: 50` → Run. It takes about 10 minutes.
3. When the `deploy` job is green, open https://registry.coffee and https://registry.coffee/catalog/.
4. Run it again with `sample` blank for the full registry (about an hour), or leave it to the Monday schedule.

Until the first deploy, registry.coffee shows GitHub's 404 page. That's expected.

## Re-running

- A weekly run is safe to re-run for the same week: it only fetches roasters that don't have a successful fetch yet.
- It uploads to the lake after each step, so a timeout or a failure late in the run keeps what was fetched.
- The `lake` concurrency group stops the weekly run and the issue-form workflow from writing at the same time.

## Upkeep

- Dependabot opens a monthly PR for the pinned actions and `uv.lock`. Merge it when CI is green.
- Rotate `HF_TOKEN` yearly, or immediately if it may have leaked: create the new token, update the secret, then
  delete the old token on Hugging Face.
