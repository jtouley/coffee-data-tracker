# Coffee Data

A weekly catalog of what **US specialty coffee roasters** are selling, plus a **personal espresso log** linked to it. The data is modeled with dbt + DuckDB and published as a static site. It runs on GitHub Actions; the data lives in a **data lake** (a Hugging Face dataset), not in git.

It's a data project first:

- **Registry:** about 3,200 US roaster websites, discovered from open datasets (Overture Maps, OpenStreetMap).
- **Platforms:** each site is probed for a storefront that publishes its catalog: Shopify, WooCommerce, Squarespace or Square Online, with a schema.org fallback for everything else.
- **Catalog:** every week, every roaster's full catalog is recorded as-is. Price history, delistings and new arrivals are *derived* from those weekly records.
- **Enrichment:** origin, process, varietal, elevation, roast level and tasting notes are extracted with rules first. Claude Haiku (optional) fills in what the rules miss.
- **Shot log:** your shots link to catalog coffees, so you can ask things like "what grind works for washed Ethiopians" or "do I rate naturals higher".

## Quickstart (local)

```bash
make install                 # uv sync
make weekly SAMPLE=50        # 50 roasters into ./lake (omit SAMPLE for all ~2,700; ~1 hour)
open build/site/index.html   # or: python -m http.server -d build/site
```

`make test` runs everything offline: ruff, unit tests, and dbt builds over synthetic multi-week lakes.

## Design: an immutable lake, everything else derived

Every step **adds files and never edits or deletes one** (`src/coffeedata/lake.py` refuses to overwrite). Current state is computed, never stored:

| Lake table | Written by | One file per |
|---|---|---|
| `snapshots/run_date=…/batch=…` | ingest | fetch batch: every product seen (coffee or not), with variants and prices |
| `fetches/…` | ingest | fetch batch: one row per roaster attempt, `ok` or `error` |
| `bodies/…` | ingest | product description text, stored once per new description hash |
| `enrichments/method=rules\|llm/…` | ingest / enrich | extracted attributes, keyed by (product, description hash) |
| `probes/…` | probe | storefront platform detection results |
| `registry/source=…/…` | discover / issue form | roasters added (or opted out) beyond the seed list |
| `llm_batches/state=submitted\|collected/…` | enrich | Claude Message Batches bookkeeping |
| `shots/<id>__<time>.parquet` | MCP / issue form | one logged shot; corrections and retractions are new records |
| `marts/*.parquet` | build | **derived**, replaced weekly: `dim_coffee`, `dim_roaster`, `mart_market`, `fct_price_event` |

What that buys:

- **Idempotent.** Re-running a week only fetches roasters without a successful fetch that week. Duplicate batches collapse on natural keys (`stg_runs` keeps the latest successful batch per roaster and week), so derived history is unchanged; the dbt tests and `tests/test_dbt.py` check this.
- **Failures can't corrupt history.** A failed fetch is recorded as an error and contributes no products. Price events compare *consecutive successful* fetches of a roaster, so an outage never shows up as a delisting.
- **Delisted coffees keep their history.** `coffee_id` is `<roaster domain>:<store product id>`, which is stable. `dim_coffee` covers every coffee ever seen, with `is_listed`.
- **Reprocessable.** Raw records include non-coffee products and description text, so classifier or extractor improvements can be re-applied to past weeks without refetching anything.
- **Status is derived too.** A roaster's status (`active`, `no_coffee`, `no_catalog`, `pending`, `unreachable`, `opted_out`) comes from its probe and fetch history. It becomes `unreachable` only after 3 failed *weekly runs*; a flaky probe never erases a known platform.

### Where the lake lives

`COFFEEDATA_LAKE` picks it:

- **Unset:** `./lake` on local disk, which is ignored by git.
- **`hf://datasets/<owner>/<name>`:** a Hugging Face dataset. Runs stage new files under `build/lake-staging`, then upload them in **one commit** per run. That commit refuses to replace any existing file except the derived `marts/`. DuckDB and dbt read the dataset directly over `hf://`.

Why Hugging Face: datasets are free, versioned (every weekly run is a commit), built for Parquet, and queryable by anyone with DuckDB. Git is the wrong tool for this data: history only grows, there are file-size limits, every Actions checkout pulls it, and data churn buries code changes. An S3, R2 or GCS backend would be a small addition to `lake.py`; DuckDB already reads those.

Expected size, measured on a sample: about 140 bytes per product row in zstd Parquet. A full weekly run of about 80k products (coffee and non-coffee) is roughly 10–12 MB, or about 600 MB a year. Descriptions add about 10 MB once, then only when a description changes. That's comfortable for a Hugging Face dataset.

## How it works

```
config/roasters.csv (seed) + lake registry ──probe──▶ platform ──ingest──▶ snapshots · fetches · bodies · enrichments
                                                                                   │
                                     Claude Haiku (optional) ──enrich──▶ enrichments (llm)
                                                                                   │
                                                           dbt + DuckDB over the lake (stg_runs dedupes re-runs)
                                                                                   │
                     dim_coffee · dim_roaster · fct_price_event · fct_shot · mart_dial_in · mart_market · mart_new_arrivals
                                                                                   │
                                                 static site (GitHub Pages) · marts/ in the lake · MCP server (Claude)
```

`coffeedata weekly` (the `weekly.yml` workflow, Mondays) runs, in order:

1. **Discover** Overture + OSM. Only in the first week of the month, and only once that month.
2. **Probe** roasters not probed in 28 days.
3. **Ingest** every roaster with a usable platform, then retry that week's failures once.
4. **Enrich** with Claude, if a key is set: collect finished batches, then send pending coffees.
5. **Build** the dbt models and export the marts.
6. **Publish** to the lake.
7. **Build the site.**

**Enrichment.** Rules read structured tags (`origin:Panama`, `roastlevel__light-roast`), titles, and labelled body text. What still needs Claude is *derived*: a coffee whose rules result is incomplete and that has no `llm` row yet, and isn't in an in-flight batch. So anything over the cap (`--limit`, default 2000), any API error and any failed batch item is simply picked up again the following week.

- Up to 100 items go through the API synchronously.
- More go through the **Message Batches API**, at half price, and are collected on the next run.

## Setup on GitHub

1. Create a **write** token at huggingface.co → Settings → Access Tokens.
2. Create the dataset (once, from any machine). This creates the dataset (private by default; add `--public` to share it), uploads the dataset card and publishes the empty table schemas:
   ```bash
   COFFEEDATA_LAKE=hf://datasets/<you>/coffee-data HF_TOKEN=<token> uv run coffeedata init
   ```
3. In the GitHub repo settings:
   - **Variables:** `LAKE_URL` = `hf://datasets/<you>/coffee-data`
   - **Secrets:** `HF_TOKEN`
   - **Secrets (optional):** `ANTHROPIC_API_KEY`
4. Settings → Pages → Source: **GitHub Actions**.
5. Create the `shot` and `roaster` issue labels.
6. Actions → **weekly** → Run workflow. Try `sample: 50` first, then leave it to the Monday schedule.

## Logging shots

Two ways in. Both write a new shot file to the lake, match the coffee to the catalog, and reply with dial-in advice. The advice is rule-based (target about 1:2 in 25–32 s): sour or fast means grind finer, bitter or slow means coarser. To correct a shot, log it again with the same `shot_id`; to retract one, log it with `deleted: true`. The latest record wins.

**1. From Claude (MCP).** Register the server once:

```bash
claude mcp add coffee-data -e COFFEEDATA_LAKE=hf://datasets/<you>/coffee-data -e HF_TOKEN=… \
  -- uv run --directory /path/to/coffee-data-tracker coffeedata mcp
```

Then just talk: *"log a shot: Onyx Geometry, 18 in, 38 out, 29 s, grind 15, a bit sour, 3/5"*. Tools:

- `log_shot`
- `search_coffees` (by text, roaster, process, origin, in stock; reads the same `dim_coffee` the site does)
- `shot_history`
- `suggest_next`
- `run_sql` (a single read-only SELECT against the dbt models in `build/coffee.duckdb`)

**2. From your phone.** Open a new issue with the **Log a shot** form. The `issue-forms` workflow, which runs only for the repo owner's issues, writes the shot to the lake, comments with the match and advice, and closes the issue. **Add a roaster** works the same way, with a checkbox for roasters who want to be excluded.

## Example queries

Run these against `build/coffee.duckdb`, or directly against the lake's `marts/`:

```sql
-- Best grind per coffee (what worked last time?)
select label, best_grind, best_dose_g, best_yield_g, best_time_s, avg_rating, shots
from mart_dial_in where grain = 'coffee' order by last_brewed_at desc;

-- Do I rate washed or natural coffees higher?
select process, count(*) shots, round(avg(rating), 2) avg_rating from fct_shot group by 1 order by 3 desc;

-- Cheapest in-stock Ethiopian per 100 g
select roaster_name, title, price_per_100g, url from dim_coffee
where origin_country = 'Ethiopia' and in_stock and not is_blend order by price_per_100g limit 10;

-- Price changes in the last month
select run_date, coffee_id, previous_price_usd, price_usd from fct_price_event
where event = 'price' and run_date >= current_date - 30 order by run_date desc;

-- Straight from the published lake, no checkout needed
select origin_country, count(*) from 'hf://datasets/<you>/coffee-data/marts/dim_coffee.parquet'
where is_listed group by 1 order by 2 desc;
```

## Scraping etiquette

- Each roaster's own public storefront feed is fetched **once a week**, with an identifying User-Agent (`coffee-data/0.1 (+repo URL)`).
- `robots.txt` is honored, including treating a 401/403 robots.txt as "disallow everything", and requests to any one host are spaced at least a second apart.
- The only things stored are what a shopper sees: titles, prices, stock and descriptions. Every coffee links back to the roaster, and roasters can opt out.
- Directories whose terms forbid scraping (e.g. Roast Local), data that can't be stored under its terms (Google Places, Yelp), and sites that block bots are not used.

## Data sources and licenses

See [`docs/ATTRIBUTION.md`](docs/ATTRIBUTION.md):

- Overture Maps Places: CDLA-Permissive-2.0
- OpenStreetMap: ODbL, © OpenStreetMap contributors

The code is MIT-licensed.

## History

This started in March 2024 as a way to log espresso shots from a form on a Squarespace site into GCP. It used a Cloud Function, BigQuery and Terraform, and drew on a separate scraper repo with per-site Selenium and GPT-3.5 scrapers. The architecture was mostly for fun, and the pieces were never fully connected.

This version reframes it as a data project:

- Storefront JSON feeds replace the browser scraping.
- An open dataset of roaster locations replaces a hand-typed list.
- An immutable lake plus dbt + DuckDB replaces the cloud stack.
- MCP and issue forms replace the web form.

The original GCP version is commit [`92f74da`](../../tree/92f74da332184533e4c10e3dab83c9fb44e0d796) (suggested tag: `v0-gcp`). The review that led here is in [`docs/REVIEW.md`](docs/REVIEW.md).
