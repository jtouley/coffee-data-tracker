# One-shot MVP prompt: Coffee Data

> **Status:** this prompt has been carried out. The repo now *is* this design, so you don't need to run it again. It's kept as the spec, and as a template for similar projects.

Paste everything inside the fence into Claude Code, run from the root of `coffee-data-tracker`. It turns this repo into a data project with two inputs:

- **Catalog:** what specialty roasters sell, refreshed daily.
- **Brews:** the shots you pull, linked to the coffees in that catalog.

It runs on GitHub Actions, DuckDB and dbt, with no cloud bill. It supersedes `coffee-scraper`, which you can archive afterwards, and the GCP stack in `src/`.

````text
You are working in the coffee-data-tracker repo. This is a DATA project; keep infrastructure minimal.
Rebuild it in ONE pass into "Coffee Data": a daily specialty-coffee catalog scraped from roaster
websites, plus a personal espresso shot log linked to that catalog, modeled with dbt + DuckDB.
Everything runs locally or in GitHub Actions at $0. Read docs/REVIEW.md for history.

KEY FACT: most specialty roasters run Shopify, which serves the catalog as JSON at
https://<domain>/products.json?limit=250&page=N. Use it. Do NOT use Selenium, browsers, or
LLM-driven scraping. Verified working on 2026-10-26: counterculturecoffee.com, www.intelligentsia.com,
onyxcoffeelab.com, sightglasscoffee.com, www.vervecoffee.com, madcapcoffee.com, www.equatorcoffees.com,
kumacoffee.com, www.metriccoffee.com, www.passengercoffee.com, wonderstate.com, www.anodynecoffee.com,
www.reanimatorcoffee.com, www.joecoffeecompany.com, www.irvingfarm.com, www.panthercoffee.com,
www.methodicalcoffee.com, www.sterling.coffee, www.cuveecoffee.com, www.klatchcoffee.com.
Known not to work: bluebottlecoffee.com (403), ritualcoffee.com (no /products.json).

0. PRESERVE HISTORY
   - Run `git tag v0-gcp` on the current commit; it is pushed with the branch at the end.
   - Delete src/ (Terraform, Cloud Function, zip, web form) and every .DS_Store.
   - Move the project story from docs/README.md into a "History" section of the new root README.
   - The roaster registry is config/roasters.csv (already committed, ~3,200 US roasters with their
     platform already probed). Section 1b describes it. Do not hand-maintain a YAML list.

1. PROJECT LAYOUT (Python 3.12, uv, single package `coffeedata`)
   pyproject.toml (deps: httpx, pydantic>=2, pyyaml, duckdb, dbt-duckdb, anthropic, mcp, jinja2;
   dev: pytest, ruff, respx), Makefile, config/roasters.yaml, src/coffeedata/{fetch,classify,
   extract,store,shots,mcp_server,site,cli}.py, dbt/ (dbt project), data/ (committed outputs),
   tests/ with recorded fixtures.

   Makefile targets:
   - `make install`
   - `make ingest`: fetch + store, for all enabled roasters or ROASTER=slug
   - `make enrich`: LLM extraction for new or changed coffees only
   - `make build`: dbt build
   - `make site`
   - `make mcp`: run the MCP server over stdio
   - `make test`: ruff + pytest + dbt build on fixtures
   - `make all`: ingest → enrich → build → site

1b. ROASTER REGISTRY + DISCOVERY (discover.py, registry.py). Goal: as many US roasters as possible.
   - config/roasters.csv is the registry. Columns: slug, name, domain, city, state, sources, locations,
     confidence, tagged_roastery, roast_in_name, status, final_host, platform. Treat it as data:
     rows are sorted by domain, and slug is derived from the domain.
   - Seed it from scripts/discovery/: overture_roasters.py (Overture Maps Places via DuckDB over HTTPS,
     no credentials needed) and probe.py (platform detection). Move both into the package as
     `coffeedata discover overture`, `coffeedata discover osm` and `coffeedata probe`, then delete
     scripts/.
   - Discovery sources. Use only openly licensed data, and record attribution in data/ATTRIBUTION.md:
     1. Overture Maps Places (CDLA-Permissive-2.0). Use taxonomy coffee_roastery, plus coffee places
        with "roast" in the name. Run monthly against the latest release.
     2. OpenStreetMap via Overpass (ODbL; attribution required): craft=coffee_roaster or
        roastery=yes. Query one state at a time with [timeout:60], since a whole-US query times out.
        Run monthly.
     3. Manual adds: an issue form .github/ISSUE_TEMPLATE/add-roaster.yml (name + website), handled
        by the same owner-only workflow pattern as log-shot.
     DO NOT use Roast Local (its terms forbid scraping or bulk download), Google Places or Yelp (their
     terms forbid storing the data), or anything behind a login.
   - Merge and dedupe on the registrable domain, using the public-suffix list (tldextract). Strip www.,
     locations. and shop. prefixes, and union the sources. Exclude social and aggregator hosts
     (instagram, facebook, linktr.ee, toasttab, square.site, …); keep the list in config.
   - Probe weekly (`coffeedata probe --stale-days 7`). Platform detection, in this order:
     shopify (/products.json?limit=1 returns JSON), woocommerce (/wp-json/wc/store/v1/products?per_page=1),
     squarespace (HTML marker; then find the shop collection from sitemap.xml and fetch
     <collection>?format=json), then bigcommerce?, wix?, square_online?, webflow? and other from HTML
     markers.
   - Status lifecycle:
     - active: has a catalog adapter and has yielded at least 1 coffee
     - no_adapter
     - no_coffee: the catalog has 0 coffee products (cafés and chains whose shop isn't online)
     - unreachable: 3 consecutive failures
     - opted_out: a manual flag, always honored
   - Chains: locations > 10, or a known chain (Dutch Bros, Caribou, PJ's, Blank Street, …). Keep them
     but flag is_chain, so marts can exclude them.

2. INGEST (fetch.py, classify.py, store.py, adapters/)
   - Adapter interface: `fetch_catalog(roaster) -> list[CatalogItem]`, one module per platform.
     Implement shopify, woocommerce and squarespace fully.
   - For everything else, implement a generic adapter:
     - read sitemap.xml (and its sitemap index) and pick likely product URLs (/product, /shop/, /coffee/)
     - fetch at most 60 pages per roaster
     - parse schema.org Product JSON-LD (name, offers.price, description, sku); skip pages that
       have none
   - Scale: about 2,000+ roasters per day. Use a global concurrency of 16 and at most 1 in-flight
     request per host. Send If-None-Match / If-Modified-Since when the server gave an ETag or
     Last-Modified.
     - Shard roasters into 4 GitHub Actions matrix jobs by hash(slug) % 4, then merge.
     - The run must finish in under 30 minutes.
     - Write data/coverage.json with roasters by status and platform, coffees found, and failures, and
       show it on the site.
   - Keep repo growth small. Snapshots store body_hash, not body_html. The body text lives only in
     data/enriched/. If the data/ directory grows past 1 GB, move data/ to a Hugging Face dataset
     repo (note this in the README).
   - Shopify: paginate products.json until an empty page. Be polite:
     - User-Agent "coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker)"
     - 1 request per second per host, timeouts, 3 retries with backoff
     - respect robots.txt via urllib.robotparser
     - one roaster failing must never fail the run; log it and continue
   - classify.py: keep only whole-bean/ground coffee. Use deterministic rules on product_type,
     tags and title. Exclude equipment, merch, gift cards, subscriptions, capsules, cold-brew cans,
     and "sets" or bundles of more than one coffee. Unit-test the rules against fixtures from at
     least 3 roasters.
   - Normalize each coffee to a pydantic CatalogItem:
     - identity: roaster_slug, product_id, handle, url, title
     - vendor, product_type, tags, body_html, body_hash, images[0]
     - variants: variant_id, title, price_usd, grams (parsed from the variant title: "12 oz"→340,
       "2 lb"→907, "5 lb", "250g"…), available
     - first_seen, last_seen
   - Storage is "git scraping", designed so diffs stay small:
     - data/catalog/<roaster>.json: the current snapshot, sorted, pretty-printed, overwritten daily.
     - data/observations/<YYYY>/<MM>.jsonl: append ONLY when something changed — new product, price
       change, availability change, or removal. Columns: observed_at, roaster_slug, product_id,
       variant_id, event (new|price|availability|removed), price_usd, available.
   - Tests: use respx with trimmed real responses saved under tests/fixtures/. Record them live now
     for counterculturecoffee.com, onyxcoffeelab.com and www.intelligentsia.com, cut to about 10
     products each.

3. ENRICH (extract.py), optional; the pipeline must work without ANTHROPIC_API_KEY
   - Structured fields: origin_country, origin_region, producer_or_farm, process (washed|natural|
     honey|anaerobic|other), varietals[], elevation_masl_min/max, roast_level (light|medium|dark),
     tasting_notes[] (lowercase, singular), is_blend, is_decaf, confidence.
   - Order of operations:
     1. Deterministic extraction first: Shopify tags like "roastlevel__light-roast" and
        "origin__single-origin", plus regex on the body for "masl" and process words.
     2. Then call Claude Haiku (model "claude-haiku-5-5") through the Anthropic Python SDK. Get
        schema-constrained JSON (structured outputs or a forced tool call). Send the title, tags and
        body text with HTML stripped and truncated to about 4k characters.
   - Cache results in data/enriched/<roaster>/<product_id>.json, keyed by body_hash, so only
     new or changed products ever hit the API. Print the token/cost estimate at the end of a run.
   - At registry scale the first backfill is tens of thousands of products. Use the Message
     Batches API, cap each run with ENRICH_MAX (default 2000), and skip the LLM when the
     deterministic extraction already filled origin, process and notes.
   - Normalize tasting notes against a small seed lexicon in config/flavors.yaml, mapping notes to
     SCA flavor-wheel categories (fruity/berry, floral, sweet/chocolate, nutty, …). Unknown notes
     are kept as-is.
   - Tests mock the API. Test the deterministic extractors with real fixture text.

4. SHOTS (shots.py)
   - data/shots.jsonl, one pydantic Shot per line:
     - shot_id (uuid4), brewed_at, coffee_id (nullable; "<roaster_slug>:<handle>"), coffee_freetext
     - roast_date (nullable), grinder_setting, dose_g, yield_g, time_s, water_temp_c (nullable)
     - brew_method (default "espresso"), rating (1–5), taste (sour|balanced|bitter, nullable)
     - is_milk_drink, notes
   - Validation ranges: dose 5–30, yield 5–500, time 1–600, rating 1–5.
   - fuzzy_match_coffee(text) → best catalog coffee_id(s) with scores (rapidfuzz is fine).
   - suggest_next(shot history for a coffee) → deterministic dial-in advice. Example rules:
     ratio target 1:2 and time 25–32s; sour or fast → finer by one step; bitter or slow → coarser.
     Return a human-readable reason. No LLM needed.

5. LOGGING INTERFACES (both write through shots.py)
   a. MCP server (mcp_server.py, FastMCP, stdio) with tools:
      - log_shot (accepts loose input; resolve the coffee with fuzzy_match_coffee and echo the
        match back)
      - search_coffees(query, roaster?, process?, in_stock_only?)
      - shot_history(coffee_id or text)
      - suggest_next(coffee_id)
      - run_sql(sql): read-only DuckDB over the dbt models; reject anything except a single SELECT
      README shows the claude_desktop_config.json / `claude mcp add` snippet.
   b. Phone logging with zero infra: a GitHub issue form at .github/ISSUE_TEMPLATE/log-shot.yml
      (fields mirror Shot; coffee as free text) and a workflow .github/workflows/log-shot.yml.
      - The workflow runs on issues opened with the label "shot", and only when
        github.actor == the repo owner.
      - It parses the issue body with a tested parser, appends to data/shots.jsonl, commits, and
        comments the matched coffee plus suggest_next advice.
      - It then closes the issue. Invalid input → a comment explaining the error, issue left open.

6. MODEL (dbt-duckdb in dbt/, DuckDB file at build/coffee.duckdb, gitignored)
   - Sources: read_json over data/catalog/*.json, data/observations/**/*.jsonl,
     data/enriched/**/*.json and data/shots.jsonl.
   - Models:
     - staging: stg_catalog_items, stg_variants, stg_observations, stg_enriched, stg_shots
     - dim_roaster
     - dim_coffee: one row per roaster product, enriched fields, first/last seen, currently
       available, price_per_100g for the cheapest bag size
     - fct_price_event
     - fct_shot: joined to dim_coffee, with brew_ratio, days_off_roast, extraction_time_bucket
     - mart_dial_in: per coffee and per process×origin — shot count, best-rated params, avg rating
     - mart_market: coffees by origin/process/roaster, price-per-100g distribution, new arrivals
       in the last 7 and 30 days, tasting-note frequency by origin
   - dbt tests: unique and not_null keys, accepted_values for process and roast_level, relationship
     fct_shot.coffee_id → dim_coffee (warn severity, because freetext shots exist).
   - A seed or fixture mode so `make test` builds dbt from tests/fixtures without network.

7. SITE (site.py → build/site/, published to GitHub Pages)
   - One static index.html (Jinja + vanilla JS, no framework) with:
     - a searchable and filterable catalog table (roaster, origin, process, notes, price/100g,
       in stock, link)
     - "new this week"
     - 2–3 inline-SVG charts: price/100g by origin, process mix by roaster, your rating by
       brew ratio
     - your recent shots
   - Light and dark mode, mobile-friendly. Data is embedded as JSON from DuckDB queries.

8. AUTOMATION (.github/workflows/)
   - ci.yml on PRs and pushes: ruff, pytest, dbt build in fixture mode.
   - daily.yml on cron "17 11 * * *" plus workflow_dispatch:
     - make ingest, then make enrich (only if the ANTHROPIC_API_KEY secret is set)
     - commit data/ if it changed, as a "coffee-data-bot" author with a message like
       "catalog: +3 new, 5 price changes"
     - then make build site and deploy to Pages (actions/deploy-pages)
     - permissions: contents write, pages write, id-token write
   - log-shot.yml as in 5b.

9. DOCS
   - Root README:
     - what it is and why (it's a data play)
     - a quickstart: `make install && make all`, then open build/site/index.html
     - how to add a roaster
     - how to log shots (MCP and the issue form)
     - 4 example SQL queries: best grind per coffee; washed vs natural rating; cheapest Ethiopian
       per 100g in stock; tasting notes by origin
     - a scraping etiquette note
     - History
   - Rewrite docs/REVIEW.md's TL;DR to point to the new design.
   - .gitignore: build/, .venv/, __pycache__/, *.duckdb, .env, .DS_Store.
   - Add an MIT LICENSE.

CONSTRAINTS
- No cloud resources, no Docker, no Selenium. No secrets in code; the only secret is
  ANTHROPIC_API_KEY, which is optional.
- Small, typed, pure functions. Network and LLM calls are isolated in fetch.py and extract.py so
  everything else is testable offline.
- Data files must be deterministic: stable sort and key order, so daily diffs are minimal.

ACCEPTANCE (run these yourself and fix until green)
1. `make test` passes offline.
2. `make ingest ROASTER=counterculturecoffee` and two other roasters, run live, write snapshots.
   Running it a second time immediately appends zero observation rows.
3. `make build` succeeds on the live data, and every dbt test passes or warns.
   Print the row counts of dim_coffee and the top 5 rows of mart_market.
4. Add 3 sample shots via the CLI, linked to real coffees from step 2, plus one freetext shot.
   `fct_shot` and `mart_dial_in` show them. Then remove the sample shots before committing.
5. Start the MCP server and exercise search_coffees, log_shot (against a temp shots file) and
   run_sql through the mcp client in a pytest test.
6. `make site` produces build/site/index.html. Render it with Playwright at 390px and 1280px
   widths, and check there's no console error and the catalog table has rows.
7. The issue-body parser test covers a valid form and an invalid one.
8. `git grep -nE "sk-ant|/Users/|chromedriver"` returns nothing.
9. Run discovery and coverage:
   - `coffeedata probe --sample 50` runs live and agrees with the platform already in
     config/roasters.csv for at least 90% of rows.
   - `make ingest SAMPLE=100` runs over 100 random active roasters, covering every adapter, and writes
     data/coverage.json.
   - Report coffees found per adapter, and fix the adapter for any platform yielding less than 50%.

When done, commit in logical steps, push the branch and the v0-gcp tag, and tell me:
- the one-time GitHub settings to flip: Pages source = GitHub Actions, the "shot" label, the
  optional ANTHROPIC_API_KEY secret
- the `claude mcp add` command
- anything you couldn't verify
````

## After it finishes

1. In GitHub, go to Settings → Pages and set the source to **GitHub Actions**.
2. Optionally add the `ANTHROPIC_API_KEY` repo secret to turn on enrichment. Without it, the catalog uses tag and regex extraction only.
3. Register the MCP server with Claude using the `claude mcp add …` line from the final message.
4. Archive `jtouley/coffee-scraper`, since this repo now replaces it.
