# Using it

The app, the other ways to log a shot, querying the data, the commands, configuration, and running it on
GitHub. For how the pipeline works, see [ARCHITECTURE.md](ARCHITECTURE.md); for setup in the cloud, the
[`runbook/`](../runbook/README.md) and [`docs/ingestion/RUNBOOK.md`](ingestion/RUNBOOK.md).

## Log shots in the app

`app/` is a small FastAPI app with server-rendered pages, made for a phone at the espresso machine. It reads and
writes the same lake as everything else (`COFFEEDATA_LAKE`, `./lake` by default). `make install` already installs
what it needs; `uv sync --no-dev --extra app` installs it without the dev tools.

```bash
make app                     # http://127.0.0.1:8000, reloads when the code changes
HOST=0.0.0.0 make app        # reachable from a phone on the same network
PORT=8080 make app
```

- **Setup:** your grinder (its dial range and step, and whether lower numbers are finer) and your basket (its
  nominal dose). Do this first.
- **New bag:** take a photo of the label and Claude reads the roaster, coffee and roast date (needs
  `ANTHROPIC_API_KEY`, up to 10 reads per person per day), or type them as on the bag. It's matched to the catalog
  when the match is clear, which needs a built catalog (`make weekly` or `make build`).
- **Log shot:** starts from the last shot of the bag. Time, taste and rating start empty, with a "Same as last"
  button and a shot timer.
- **Saved:** shows the ratio and where the shot landed against the 25–32 s, 1:2 window, next to the bag's earlier
  shots, and says how it compares with the last one. For 5 seconds it offers **Undo**, which writes a retraction
  record; nothing is deleted.
- **Coffees and History:** every bag and every shot. A shot's page has **Correct** (the same form, saved under the
  shot's id) and **Retract**.

Each save waits in the phone's IndexedDB until the server acknowledges it, so a save made offline goes through
later, and a retry never creates a second shot. The app shows where each shot landed but gives no grind advice; that
waits until a model trained on logged shots does better than the rule the other tools use (below).

> [!WARNING]
> Without `SESSION_SECRET`, every request is the owner (`COFFEEDATA_OWNER_ID`, default `owner`), so with
> `HOST=0.0.0.0` anyone on your network can log shots. Real sign-in, invites and deployment are in
> [`docs/app/RUNNING.md`](app/RUNNING.md).

The app reads shots, bags and equipment from the lake only when it starts. To see shots logged from the CLI,
Claude (MCP) or an issue, restart the app.

<a id="logging-shots"></a>

## Log shots elsewhere

Every way of logging a shot writes it to the lake and matches it to a catalog coffee. The CLI, Claude (MCP) and the
issue form also reply with advice for the next shot, aiming for about 1:2 in 25–32 s: a sour shot means grind finer
and a bitter one coarser; with no taste given, a fast shot means finer and a slow one coarser. Which way is finer
comes from the Setup of the last shot's grinder, or lower numbers if none is set.

**From the command line:**

```bash
uv run coffeedata shot add --json '{"coffee": "Onyx Geometry", "in": 18, "out": 38, "time": 29, "grind": 15, "rating": 3, "taste": "sour"}'
```

```text
Logged shot `<shot id>`.

Last shot: 18 g → 38 g (1:2.1) in 29 s at grind 15. It tasted sour: go finer to 14 to slow the shot and extract more.
```

Once the catalog is built, the reply also names the matched coffee, or the closest candidates if the match isn't
certain. Add `"grinder_name": "DF64 V2"` to tie the shot to a grinder from the app's Setup. The key `"grinder"`
is an alias for `grind`, the grind setting.

To correct a shot, run `shot add` again with its `shot_id` and every field (include `brewed_at`, or it becomes the
current time); the latest record wins. Add `"deleted": true` to such a full record to retract it.

**From Claude**, register the MCP server once (for a local lake, leave out both `-e` flags):

```bash
claude mcp add coffee-data -e COFFEEDATA_LAKE=hf://datasets/<you>/coffee-data -e HF_TOKEN=<token> \
  -- uv run --directory /path/to/coffee-data-tracker coffeedata mcp
```

Then say something like "log a shot: Onyx Geometry, 18 in, 38 out, 29 s, grind 15 on the DF64 V2, a bit sour,
3/5". The server has `log_shot` (with an optional `grinder_name`), `search_coffees`, `shot_history`,
`suggest_next` and `run_sql` (one read-only `SELECT` against the models in `build/coffee.duckdb`).

`log_shot` asks you to confirm each shot before it is written (MCP elicitation), because the same conversation also
reads roasters' own text through `search_coffees`, which returns it marked as data. A client that can't ask is
refused. To allow writes from one anyway, leaving its own tool approval as the check, add
`-e COFFEEDATA_MCP_WRITES=1` when you register the server.

**From GitHub**, open an issue with the "Log a shot" form; its optional Grinder field takes a grinder name. The
`issue-forms` workflow records the shot, replies with the match and advice, and closes the issue. It only acts on
issues the repo owner opens.

A grinder name is looked up among the owner's grinders in the same lake, so run the app against the lake the workflow and
the MCP server use. An unknown name fails with the list of known grinders.

| | App | CLI | Claude (MCP) | Issue form |
|---|---|---|---|---|
| Advice for the next shot | no | yes | yes | yes |
| Correct a shot | Correct, on the shot's page | log the full record again with its `shot_id` | no | no |
| Retract a shot | Undo, for 5 s after saving, or Retract on the shot's page | the full record plus `"deleted": true` | no | no |

## Query the data

DuckDB comes with the project's dependencies, so you can query the models without installing anything else:

```bash
uv run python -c "import duckdb; duckdb.connect('build/coffee.duckdb', read_only=True).sql(\"\"\"
  select origin_country, count(*) as coffees from dim_coffee
  where is_listed group by 1 order by 2 desc limit 10\"\"\").show()"
```

```sql
-- Cheapest in-stock single-origin Ethiopians per 100 g
select roaster_name, title, price_per_100g, url from dim_coffee
where origin_country = 'Ethiopia' and in_stock and not is_blend order by price_per_100g limit 10;

-- Price changes in the last month
select run_date, coffee_id, previous_price_usd, price_usd from fct_price_event
where event = 'price' and run_date >= current_date - 30 order by run_date desc;

-- What worked last time, per coffee and grinder (needs logged shots)
select label, grinder_name, best_grind, best_dose_g, best_yield_g, best_time_s, avg_rating, shots
from mart_dial_in where grain = 'coffee' order by last_brewed_at desc;

-- Washed vs natural ratings
select process, count(*) shots, round(avg(rating), 2) avg_rating from fct_shot group by 1 order by 3 desc;

-- Straight from a Hugging Face lake
select origin_country, count(*) from 'hf://datasets/<you>/coffee-data/marts/dim_coffee.parquet'
where is_listed group by 1 order by 2 desc;
```

| Model | One row per |
|---|---|
| `dim_coffee` | coffee ever seen, with attributes, sizes, price per 100 g, `in_stock`, `is_listed` and `catalog_status` (available / preorder / sold_out / archived / delisted) |
| `dim_roaster` | roaster, with platform and status |
| `fct_price_event` | change: `new`, `price`, `availability`, `removed` or `relisted` |
| `fct_shot` | current shot, with its bag, grinder (`grind_position` compares across grinders) and catalog coffee |
| `dim_equipment` | grinder, machine or basket, retired ones included |
| `mart_dial_in` | your best recipe per coffee and grinder, and per origin and process |
| `mart_bag` | bag, with its shot count, best shot and days off roast |
| `mart_market`, `mart_new_arrivals` | market summary by origin and process; coffees new in each roaster's latest run |

`dim_coffee`, `dim_roaster`, `mart_market`, `fct_price_event`, `fct_shot` and `mart_bag` are also exported to the
lake's `marts/` each run. `fct_shot` and `mart_bag` hold your shots and bags, so keep the lake private (`coffeedata init` makes it private unless
you pass `--public`).

## Commands

| `make` target | Runs | What it does |
|---|---|---|
| `make weekly [SAMPLE=N]` | `coffeedata weekly` | The full weekly run (see [How it works](ARCHITECTURE.md)) |
| `make app` | `uvicorn app.main:app` | The shot-logging app; `HOST` and `PORT` default to `127.0.0.1` and `8000` |
| `make init` | `coffeedata init` | Create the lake. For `hf://`, a private dataset, its card and schemas (`make init PUBLIC=1` for a public one) |
| `make discover` | `coffeedata discover overture`, `discover osm` | Record roasters from open datasets |
| `make probe` | `coffeedata probe` | Detect the store platform for roasters not checked in 28 days |
| `make ingest [SAMPLE=N]` | `coffeedata ingest` | Fetch catalogs for today's run, skipping roasters already fetched |
| `make enrich` | `coffeedata enrich` | Claude enrichment (needs `ANTHROPIC_API_KEY`) |
| `make build` / `make site` | `coffeedata build` / `site` | dbt build and mart export / the static site (runs `build` first) |
| `make publish` | `coffeedata publish` | Upload staged files to a remote lake |
| `make mcp` | `coffeedata mcp` | Run the MCP server over stdio |
| `make contracts` | `python -m coffeedata.raw_contracts --all` | Regenerate the internal ODCS contracts at their versions; refuses a change to names, types, required or enums without a new version |

The CLI also has `build-diff`, `build-hashes`, `override`, `export-public --to <dir|hf://|s3://|gs://>`, `load-snowflake` (`make install` includes the
connector; with `--no-dev`, add `--extra snowflake`; it also writes the table, view and column descriptions
that make the Snowflake listing's data dictionary, from the public contract), `shot add|from-issue` and `add-roaster`. `uv run coffeedata
<command> --help` lists each command's flags.

## Configuration

Everything is set through environment variables. Nothing is required for a local run.

| Variable | Default | Used for |
|---|---|---|
| `COFFEEDATA_LAKE` | `lake` | Where the lake lives: a local folder, or `hf://datasets/<owner>/<name>` |
| `HF_TOKEN` | none | Reading and writing a Hugging Face lake or public export |
| `COFFEEDATA_STAGING` | `build/lake-staging` | Where new files wait before upload to a remote lake |
| `COFFEEDATA_DB` | `build/coffee.duckdb` | The DuckDB file the MCP server reads. `build` and `weekly` take `--db` instead (same default); `site` and `export-public` default to `build/served.duckdb`, the served release |
| `COFFEEDATA_PHOTOS` | `lake-photos` | A separate private dataset for the app's bag photos |
| `COFFEEDATA_USERS` | `lake-users` | A separate private dataset for the app's accounts and invites |
| `COFFEEDATA_IMPORTERS` | `lake-importers` | A separate private dataset for weekly snapshots of green importers' public offer lists (`coffeedata importer-snapshot`, `.github/workflows/importers.yml`); never read by the main dbt project or released. Only the private one (`dbt_importers/`, `coffeedata importer-build`) reads it, and `coffeedata importer-build --publish` keeps its latest `importer_offers` there under `derived/` |
| `COFFEEDATA_IMPORTERS_DB` | none | The private importers project's database file. `coffeedata importer-build` sets it to `build/importers.duckdb` under the checkout (whatever the working directory; dbt runs from there), its only allowed value; the project refuses any other, and the command refuses a public database, a link (the file or its `build/` folder), or a file holding tables it doesn't own (dbt's own work tables for its models, left by an interrupted build, aside) |
| `SESSION_SECRET`, `AUTH_PEPPER`, `OWNER_SETUP_CODE` | none | The app's sign-in; see [`docs/app/RUNNING.md`](app/RUNNING.md) |
| `COFFEEDATA_LABEL_MODEL` | `DEFAULT_MODEL` in `app/labels.py` | The model that reads bag labels (`app/eval/bag_read.py` compares Opus and Haiku) |
| `COFFEEDATA_OWNER_ID` | `owner` | The owner's `user_id`; shots, bags and equipment with no `user_id` are the owner's |
| `COFFEEDATA_OWNER` | `owner` | The owner's display name in the app |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Where `make app` listens |
| `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` | none | Claude enrichment. Without it, attributes come from the rules only. The app's bag-label reading needs `ANTHROPIC_API_KEY` |
| `SITE_URL`, `SUPPORT_EMAIL`, `GOVERNING_STATE` | `https://registry.coffee`, `support@registry.coffee`, `Michigan` | The site's canonical URL, and the contact and state on its legal pages. The app links to the same `SITE_URL` and sends beta-invite requests to `SUPPORT_EMAIL` (set on Cloud Run by Terraform, `site_url` and `support_email`) |
| `APP_URL` | `https://app.registry.coffee` | The app the site's Sign in links point to (a fork or a staging build) |
| `PUBLIC_EXPORT_URL` | `build/public` | Where `weekly` and `export-public` send the public tables |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY` | none | Snowflake load. With `SNOWFLAKE_ACCOUNT` set, the Tuesday `site` workflow (and a manual `weekly` run) loads Snowflake after the site (`load-snowflake`; `--migrate` after a public contract change). A local `coffeedata weekly` doesn't: run `coffeedata load-snowflake` after it |
| `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE` | `COFFEE_LOADER`, `COFFEE_WH`, `COFFEE_DATA` | Snowflake overrides |
| `SNOWFLAKE_SHARE` | `COFFEE_DATA_SHARE` | The share whose views `load-snowflake --migrate` recreates and grants again |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | none | `s3://` exports (any DuckDB AWS credential source works) |
| `GCS_HMAC_KEY_ID`, `GCS_HMAC_SECRET` | none | `gs://` exports |

The seed list is [`config/roasters.csv`](../config/roasters.csv): roasters from Overture Maps and a curated list.
Roasters added later by discovery (including OpenStreetMap) or the issue form are recorded in the lake, and this
file stays as it is. The site counts them all each week ("roasters in the registry" on
[registry.coffee](https://registry.coffee/)).

## Run it on GitHub

The weekly crawl runs every Monday on Cloud Run ([`docs/ingestion/RUNBOOK.md`](ingestion/RUNBOOK.md)), and
the `site` workflow publishes the site to Pages on Tuesday. The `weekly` workflow is the manual fallback: run by
hand, it keeps the data in a Hugging Face dataset and deploys the site to Pages.

1. Create a write token at huggingface.co → Settings → Access Tokens.
2. Create the dataset once, from any machine. It's private unless you add `--public`; keep it private, because
   the run exports your shots to its `marts/fct_shot.parquet`.
   ```bash
   COFFEEDATA_LAKE=hf://datasets/<you>/coffee-data HF_TOKEN=<token> uv run coffeedata init
   ```
3. In the repo's Actions settings, add the variable `LAKE_URL` = `hf://datasets/<you>/coffee-data` and the secret
   `HF_TOKEN`. Add the secret `COFFEEDATA_ANTHROPIC_API_KEY` and the variable `VERDICT_MODEL` (`claude-haiku-5-5`): the run fails without them (a rules-only catalog is for a local lake only; on a remote lake it is refused). Optionally add the variable `PUBLIC_EXPORT_URL` (for `s3://`, also
   the AWS secrets and the `AWS_REGION` variable); and for Snowflake the variables `SNOWFLAKE_ACCOUNT` and
   `SNOWFLAKE_USER` and the secret `SNOWFLAKE_PRIVATE_KEY` (see [`marketplace/README.md`](../marketplace/README.md));
   and for the site the variables `SITE_URL`, `SUPPORT_EMAIL`, `GOVERNING_STATE` and `APP_URL`.
4. Under Settings → Pages, set Source to GitHub Actions.
5. Create the `shot` and `roaster` issue labels. The issue forms rely on them.
6. Run the `weekly` workflow by hand with `sample: 50`. It has no schedule: the Cloud Run jobs crawl on Monday and
   the `site` workflow publishes on Tuesday. A fork without the Cloud Run jobs ([`docs/ingestion/RUNBOOK.md`](ingestion/RUNBOOK.md))
   has no scheduled crawl: run `weekly` by hand each Monday, or the Tuesday `site` run fails its freshness check.

The `deploy` workflow runs when a change touches the app. On a pull request it builds the app's image and checks
its Terraform; on a push to `main` it also deploys it to Cloud Run at `app.registry.coffee`. It skips the deploy
until the `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA` and `GCP_IMAGE` variables are set; setup and the app's secrets are in
[`docs/app/RUNNING.md`](app/RUNNING.md). To log shots from a local app into the same lake, start
`make app` with `COFFEEDATA_LAKE` and `HF_TOKEN` set. Each save is then uploaded before the app acknowledges it.

## The site

The Tuesday `site` workflow deploys a static site to GitHub Pages at [registry.coffee](https://registry.coffee): a launch page at
`/` and the catalog at `/catalog/`. Setup, DNS, costs and security are in [`runbook/`](../runbook/README.md).

Its coffees, prices and charts cover the same roasters as the public tables (`public_roaster`: not opted out, not
known only from OpenStreetMap). The roaster status counts and the registry total are the exception: they count every
roaster in the registry, opted-out and OpenStreetMap-only ones included. For search engines, both pages carry a schema.org
[Dataset](https://developers.google.com/search/docs/appearance/structured-data/dataset) in JSON-LD, built from the
public contract at each build: name and description, the data licence
([`licenses/CDLA-Permissive-2.0.txt`](../licenses/CDLA-Permissive-2.0.txt), CDLA-Permissive-2.0), coverage (US, from
the 2026-10-26 run on), every contract column as a variable, the downloads (`catalog.json`, and the Hugging Face
files when `PUBLIC_EXPORT_URL` is an `hf://` dataset), and the release (`dateModified` is its run date, `version` its build id). The site also has a `robots.txt` that allows
everything and a `sitemap.xml` of its pages. `catalog/catalog.json` starts with a `release` object (build id, run
date, contract id and version, licence) before its `columns` and `rows`. Each row is one coffee; columns are only ever added
at the end (`roaster_id` and `roaster_city`, the roaster's city as `public_roaster` stores it, came after `first_seen`),
so look them up by name in `columns`.
