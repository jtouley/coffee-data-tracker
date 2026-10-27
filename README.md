# Coffee Data

A weekly catalog of what US specialty coffee roasters sell online, and an espresso log that links each shot to a
coffee in that catalog. It runs on GitHub Actions. The data lives in a Hugging Face dataset, not in git, and is
modeled with dbt and DuckDB.

- About 3,200 US roaster websites, found in Overture Maps and OpenStreetMap.
- Each site is checked for a storefront that publishes its catalog (Shopify, WooCommerce, Squarespace, Square
  Online), with schema.org product markup as a fallback.
- Every week each roaster's full catalog is saved as-is. Price changes, delistings and new arrivals are computed
  from those weekly records.
- Origin, process, varietal, elevation, roast level and tasting notes come from tags and text rules. If you add an
  Anthropic key, Claude Haiku fills in what the rules miss.
- Shots are linked to catalog coffees, so you can ask "what grind works for washed Ethiopians?" or "do I rate
  naturals higher?"

## Quickstart

```bash
make install
make weekly SAMPLE=50        # 50 roasters into ./lake; leave out SAMPLE for all of them (about an hour)
open build/site/index.html
```

`make test` runs the lint checks, the unit tests and dbt builds over synthetic multi-week lakes, all offline.

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.12. Run `make hooks` once to install the pre-commit
hooks: whitespace and YAML/TOML checks, a check that `uv.lock` matches `pyproject.toml`, ruff (lint and format) and
mypy. `make lint` runs the same hooks on every file, and so does CI.

## Design

Each step adds files to the lake and never edits or deletes one; `src/coffeedata/lake.py` refuses to overwrite.
Anything that describes the current state is computed from those files.

| Lake table | Written by | One file per |
|---|---|---|
| `snapshots/run_date=…/batch=…` | ingest | fetch batch: every product seen (coffee or not), with variants and prices |
| `fetches/…` | ingest | fetch batch: one row per roaster attempt, `ok` or `error` |
| `bodies/…` | ingest | product description text, stored once per new description |
| `enrichments/method=rules\|llm/…` | ingest, enrich | extracted attributes, keyed by product and description hash |
| `probes/…` | probe | storefront platform checks |
| `registry/source=…/…` | discover, issue form | roasters added (or opted out) beyond the seed list |
| `llm_batches/state=submitted\|collected/…` | enrich | Claude Message Batches bookkeeping |
| `shots/<id>__<time>.parquet` | MCP, issue form | one logged shot; corrections and retractions are new records |
| `marts/*.parquet` | build | derived and replaced weekly: `dim_coffee`, `dim_roaster`, `mart_market`, `fct_price_event` |

Some consequences:

- Re-running a week only fetches roasters that don't have a successful fetch for it yet. `stg_runs` keeps the
  latest successful batch per roaster and week, so duplicate batches don't change the history. `tests/test_dbt.py`
  checks this.
- A failed fetch is recorded as an error with no products. Price events compare consecutive successful fetches, so
  a site being down never shows up as a delisting.
- `coffee_id` is `<roaster domain>:<store product id>`, which doesn't change when a coffee is renamed. `dim_coffee`
  keeps every coffee ever seen, with `is_listed`.
- The raw records keep non-coffee products and description text, so a better classifier or extractor can be
  re-run over past weeks without fetching anything again.
- A roaster's status (`active`, `no_coffee`, `no_catalog`, `pending`, `unreachable`, `opted_out`) comes from its
  probe and fetch history. It only becomes `unreachable` after 3 failed weekly runs, and a failed probe never
  erases a platform that was already known.

### Where the lake lives

`COFFEEDATA_LAKE` sets it. Unset, it's `./lake` (ignored by git). Set to `hf://datasets/<owner>/<name>`, new files
are staged under `build/lake-staging` and uploaded in a commit at each checkpoint: every 200 roasters during ingest,
and after each step of the weekly run. Uploads retry with backoff, and a commit can't replace an existing file
other than the derived `marts/`. DuckDB and dbt read the dataset directly over `hf://`.

Hugging Face datasets are free, versioned and built for Parquet, and anyone with DuckDB can query them. Git isn't a
good home for this data: the history only grows, every Actions checkout downloads it, and data commits bury code
changes. An S3, R2 or GCS backend would be a small change to `lake.py`.

Product rows are about 140 bytes each in zstd Parquet. A weekly run of about 80k products is 10–12 MB, or roughly
600 MB a year. Descriptions add about 10 MB up front, then only when one changes.

## How it works

```
config/roasters.csv + lake registry ──probe──▶ platform ──ingest──▶ snapshots, fetches, bodies, enrichments
                                                                           │
                                   Claude Haiku (optional) ──enrich──▶ enrichments (llm)
                                                                           │
                                                      dbt + DuckDB over the lake
                                                                           │
                 dim_coffee, dim_roaster, fct_price_event, fct_shot, mart_dial_in, mart_market, mart_new_arrivals
                                                                           │
                                          static site (GitHub Pages), marts/ in the lake, MCP server
```

`coffeedata weekly` (the `weekly.yml` workflow, Mondays) runs:

1. Discovery from Overture and OSM, in the first week of each month only.
2. Platform checks for roasters not checked in 28 days.
3. Ingest of every roaster with a usable platform, then one retry of that week's failures.
4. Claude enrichment, if a key is set: collect finished batches, then submit pending coffees.
5. The dbt build, and export of the marts.
6. The public tables, plus the Snowflake load if it's configured.
7. The site.

It uploads to the lake after steps 2 through 5, so a failure in a later step or a timeout keeps what was already
fetched. Running the workflow again for the same week picks up where it stopped.

Enrichment works out what still needs Claude each time: coffees the rules couldn't fully read that have no `llm`
row and aren't in a pending batch. Anything over the cap (`--limit`, default 2000), any API error and any failed
batch item gets picked up the following week. Up to 100 coffees go through the API directly; more go through the
Message Batches API, which costs half as much, and are collected on the next run.

## Setup on GitHub

1. Create a write token at huggingface.co → Settings → Access Tokens.
2. Create the dataset once, from any machine. It's private unless you add `--public`.
   ```bash
   COFFEEDATA_LAKE=hf://datasets/<you>/coffee-data HF_TOKEN=<token> uv run coffeedata init
   ```
3. In the repo's Actions settings, add:
   - variable `LAKE_URL` = `hf://datasets/<you>/coffee-data`
   - secret `HF_TOKEN`
   - optionally, secret `ANTHROPIC_API_KEY`
   - optionally, for Snowflake: variables `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_USER`, secret `SNOWFLAKE_PRIVATE_KEY`
     (see `marketplace/README.md`)
4. Settings → Pages → Source: GitHub Actions.
5. Create the `shot` and `roaster` issue labels.
6. Run the weekly workflow by hand with `sample: 50`. After that it runs every Monday.

## Public tables and marketplaces

Four facts-only tables are built every week (`dbt/models/public/`): coffees with their attributes, sizes, prices,
stock and a link back to the roaster; the weekly change log; a market summary by origin and process; and the
roasters covered. A dbt test fails the build if descriptions, images, store ids or shot data ever get into them.
They also leave out opted-out roasters and roasters known only from OpenStreetMap, whose ODbL license would
otherwise apply to the whole table.

With `PUBLIC_EXPORT_URL` set, the weekly run exports them to a public Hugging Face dataset or to S3/GCS.
`coffeedata export-public --to …` does the same by hand. Setup scripts for Snowflake and Databricks, and the
listing text, are in [`marketplace/`](marketplace/README.md).

## Logging shots

Shots can come from Claude or from a GitHub issue. Either way the shot is written to the lake, matched to a catalog
coffee, and you get advice for the next one. The advice is simple: aim for about 1:2 in 25–32 s; sour or fast
means grind finer, bitter or slow means coarser. To correct a shot, log it again with the same `shot_id`. To remove
one, log it with `deleted: true`. The latest record wins.

From Claude, register the MCP server once:

```bash
claude mcp add coffee-data -e COFFEEDATA_LAKE=hf://datasets/<you>/coffee-data -e HF_TOKEN=… \
  -- uv run --directory /path/to/coffee-data-tracker coffeedata mcp
```

Then say something like "log a shot: Onyx Geometry, 18 in, 38 out, 29 s, grind 15, a bit sour, 3/5". The server
has `log_shot`, `search_coffees`, `shot_history`, `suggest_next` and `run_sql` (one read-only SELECT against the
dbt models in `build/coffee.duckdb`).

From your phone, open an issue with the "Log a shot" form. The `issue-forms` workflow only acts on the repo
owner's issues: it records the shot, replies with the match and advice, and closes the issue. "Add a roaster"
works the same way and has a checkbox for roasters who want to be left out.

## Example queries

Against `build/coffee.duckdb`, or the lake's `marts/`:

```sql
-- What worked last time, per coffee
select label, best_grind, best_dose_g, best_yield_g, best_time_s, avg_rating, shots
from mart_dial_in where grain = 'coffee' order by last_brewed_at desc;

-- Washed vs natural ratings
select process, count(*) shots, round(avg(rating), 2) avg_rating from fct_shot group by 1 order by 3 desc;

-- Cheapest in-stock single-origin Ethiopian per 100 g
select roaster_name, title, price_per_100g, url from dim_coffee
where origin_country = 'Ethiopia' and in_stock and not is_blend order by price_per_100g limit 10;

-- Price changes in the last month
select run_date, coffee_id, previous_price_usd, price_usd from fct_price_event
where event = 'price' and run_date >= current_date - 30 order by run_date desc;

-- Straight from the lake
select origin_country, count(*) from 'hf://datasets/<you>/coffee-data/marts/dim_coffee.parquet'
where is_listed group by 1 order by 2 desc;
```

## Scraping etiquette

- Each roaster's public storefront feed is read once a week, with a User-Agent that names this project.
- `robots.txt` is followed (a 401 or 403 on it counts as "disallow everything"), and requests to a host are at
  least a second apart.
- Only what a shopper can see is stored: titles, prices, stock and descriptions. Every coffee links back to the
  roaster, and roasters can opt out.
- Directories whose terms forbid scraping (Roast Local), sources whose terms forbid storing their data (Google
  Places, Yelp) and sites that block bots aren't used.

## Data sources and licenses

Roasters come from Overture Maps Places (CDLA-Permissive-2.0) and OpenStreetMap (ODbL, © OpenStreetMap
contributors). Details are in [`docs/ATTRIBUTION.md`](docs/ATTRIBUTION.md). The code is MIT-licensed.

## Design lineage

Almost none of the original code is left (`git blame` puts 2 lines of the current tree on it), but the goal and
the data model carried over:

| Original (`v0-gcp`, `coffee-scraper`) | Now |
|---|---|
| Collect brewing data and analyze it in the cloud | Same goal, analyzed with dbt and DuckDB over the lake |
| Squarespace form: roaster, roast, origin, grind, dose, yield, time, rating, `is_latte`, notes | The `Shot` model and "Log a shot" form, with the same fields (`is_latte` is now `is_milk_drink`) plus taste, water temperature, roast date and brew method |
| The form's 59-roaster dropdown | The first entries in `config/roasters.csv`, which now has about 3,200 roasters |
| The form's 49-country origin dropdown | The country list in `origins.py` |
| BigQuery table with a typed schema, column descriptions, `created_at` and `received_at` | Typed Parquet schemas in `lake.py`, `brewed_at` and `logged_at` on shots, `fetched_at` on catalog records, documented dbt models |
| Cloud Function receiving form posts | The MCP server and GitHub issue forms |
| Terraform | The dbt models, workflows and lake layout, all in the repo |
| `coffee-scraper`'s Selenium and ScrapeGraph scrapers for Counter Culture, Intelligentsia, Onyx and Blue Bottle | Storefront adapters; the Shopify one covers the first three, and Blue Bottle blocks bots |
| `coffee-scraper`'s fields: name, price, roast, tasting notes, origin | `dim_coffee` and `public_coffee`, which add process, varietals, elevation, bag sizes, price per 100 g, stock and history |

## History

This started in March 2024 as a way to log espresso shots from a form on a Squarespace site into GCP, using a Cloud
Function, BigQuery and Terraform, alongside a separate repo of per-site Selenium and GPT-3.5 scrapers. The
architecture was mostly for fun and the pieces were never fully connected. The rewrite kept the idea and replaced
the parts: storefront JSON feeds instead of browser scraping, an open dataset of roasters instead of a hand-typed
list, a Parquet lake with dbt and DuckDB instead of the cloud stack, and MCP and issue forms instead of the web form.

The GCP version is commit [`92f74da`](../../tree/92f74da332184533e4c10e3dab83c9fb44e0d796).
