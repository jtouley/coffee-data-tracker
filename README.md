# Coffee Data

A daily catalog of what **US specialty coffee roasters** are selling, plus a **personal espresso log** linked to it. The data is modeled with dbt + DuckDB and published as a static site. It runs on GitHub Actions and costs nothing to host.

It's a data project first:

- **Registry:** about 3,200 US roaster websites, discovered from open datasets (Overture Maps, OpenStreetMap).
- **Platforms:** each site is probed for a storefront that publishes its catalog: Shopify, WooCommerce or Squarespace, with a schema.org fallback for everything else.
- **Catalog:** each day, every roaster's coffees are snapshotted. Price and stock changes are kept as a change log, so git history becomes price history.
- **Enrichment:** origin, process, varietal, elevation, roast level and tasting notes are extracted with rules first. Claude Haiku (optional) fills in what the rules miss.
- **Shot log:** your shots link to catalog coffees, so you can ask things like "what grind works for washed Ethiopians" or "do I rate naturals higher".

## Quickstart

```bash
make install          # uv sync
make ingest SAMPLE=50 # 50 random roasters (omit SAMPLE for all ~2,700; ~1 hour)
make site             # dbt build + static site
open build/site/index.html   # or: python -m http.server -d build/site
```

`make test` runs everything offline: ruff, unit tests, and a dbt build on recorded fixtures.

## How it works

```
config/roasters.csv ──probe──▶ platform ──ingest──▶ data/catalog/<roaster>.json      (current snapshot)
  ▲  (Overture, OSM,                               data/observations/YYYY/MM.jsonl  (new/price/stock/removed)
  │   issue form)                                  data/enriched/<roaster>.json     (attributes, cached by body hash)
  └── discover                                     data/shots.jsonl                 (your shots)
                                                          │
                                         dbt + DuckDB ────┴──▶ dim_coffee, dim_roaster, fct_price_event,
                                                                fct_shot, mart_dial_in, mart_market, mart_new_arrivals
                                                                         │
                                                         static site (GitHub Pages) · MCP server (Claude)
```

| Step | Command | Schedule |
|---|---|---|
| Discover new roasters (Overture + OSM) | `make discover` | monthly (`registry.yml`) |
| Re-detect storefront platforms | `make probe` | weekly (`registry.yml`) |
| Fetch catalogs, store, enrich | `make ingest` | daily (`daily.yml`) |
| Merge finished Claude batches | `make collect` | daily, before ingest |
| Models + site | `make site` | daily, deployed to Pages |

**Platforms.** On 2026-10-26 the registry probe found 1,070 Shopify, 233 WooCommerce and 339 Squarespace roasters. That's about half of all reachable US roasters with a clean JSON catalog. The rest (Wix, Square Online, custom sites) go through the generic sitemap + JSON-LD adapter, which works for Wix but not for sites that render their store in JavaScript. `data/coverage.json` and the site's Coverage section show where things stand after each run.

**Enrichment.** Rules read structured tags (`origin:Panama`, `roastlevel__light-roast`), titles, and labelled body text. When origin, process or notes are still missing and `ANTHROPIC_API_KEY` is set, the listing goes to **Claude Haiku** with a strict JSON schema:

- Runs with up to 100 items call the API synchronously.
- Bigger backfills go through the **Message Batches API**, at half price, and are merged on the next run.
- `--enrich-max` (default 2000) caps how many products are sent per run.
- Results are cached by a hash of the product description, so a product is only re-sent when its description changes.

## Logging shots

Two ways in. Both write to `data/shots.jsonl`, match the coffee to the catalog, and reply with dial-in advice. The advice is rule-based (target about 1:2 in 25–32 s): sour or fast means grind finer, bitter or slow means coarser.

**1. From Claude (MCP).** Register the server once:

```bash
claude mcp add coffee-data -- uv run --directory /path/to/coffee-data-tracker coffeedata mcp
```

Then just talk: *"log a shot: Onyx Geometry, 18 in, 38 out, 29 s, grind 15, a bit sour, 3/5"*. Tools:

- `log_shot`
- `search_coffees` (by text, roaster, process, origin, in stock)
- `shot_history`
- `suggest_next`
- `run_sql` (a single read-only SELECT against the dbt models)

**2. From your phone.** Open a new issue with the **Log a shot** form. The `issue-forms` workflow, which runs only for the repo owner's issues, logs the shot, commits it, comments with the match and advice, and closes the issue. **Add a roaster** works the same way.

## Example queries

```sql
-- Best grind per coffee (what worked last time?)
select label, best_grind, best_dose_g, best_yield_g, best_time_s, avg_rating, shots
from mart_dial_in where grain = 'coffee' order by last_brewed_at desc;

-- Do I rate washed or natural coffees higher?
select process, count(*) shots, round(avg(rating), 2) avg_rating from fct_shot group by 1 order by 3 desc;

-- Cheapest in-stock Ethiopian per 100 g
select roaster_name, title, price_per_100g, url from dim_coffee
where origin_country = 'Ethiopia' and in_stock and not is_blend order by price_per_100g limit 10;

-- Tasting language by origin
select origin_country, top_notes_for_origin, coffees from mart_market where process <> 'unknown' order by coffees desc;
```

## Adding roasters

- **One-off:** use the **Add a roaster** issue form, or append a row to `config/roasters.csv` and run `coffeedata probe --only <slug>`.
- **In bulk:** run `coffeedata discover overture` (latest Overture release) and `coffeedata discover osm`.
- **Opting out:** set `status` to `opted_out` and the roaster is never fetched again.

## Scraping etiquette

- Each roaster's own public storefront feed is fetched **once a day**, with an identifying User-Agent (`coffee-data/0.1 (+repo URL)`).
- `robots.txt` is honored, and requests to any one host are spaced at least a second apart.
- The only things stored are what a shopper sees: titles, prices, stock and descriptions. Every coffee links back to the roaster.
- Directories whose terms forbid scraping (e.g. Roast Local) and data that can't be stored under its terms (Google Places, Yelp) are not used.

## Data sources and licenses

See [`data/ATTRIBUTION.md`](data/ATTRIBUTION.md):

- Overture Maps Places: CDLA-Permissive-2.0
- OpenStreetMap: ODbL, © OpenStreetMap contributors

The code is MIT-licensed.

## History

This started in March 2024 as a way to log espresso shots from a form on a Squarespace site into GCP. It used a Cloud Function, BigQuery and Terraform, and drew on a separate scraper repo with per-site Selenium and GPT-3.5 scrapers. The architecture was mostly for fun, and the pieces were never fully connected.

This version reframes it as a data project:

- Shopify-style JSON feeds replace the browser scraping.
- An open dataset of roaster locations replaces a hand-typed list.
- dbt + DuckDB replaces the cloud stack.
- MCP and issue forms replace the web form.

The original GCP version is tagged [`v0-gcp`](../../tree/v0-gcp). The review that led here is in [`docs/REVIEW.md`](docs/REVIEW.md).
