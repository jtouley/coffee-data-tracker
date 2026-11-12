# Publishing the public tables

The weekly run builds four facts-only dbt models (`dbt/models/public/`). The `public_tables_are_facts_only`
dbt test fails the build if any of them ever exposes descriptions, images, store ids or shot-log columns.

- Product descriptions, images, handles, tags and the shot log are excluded.
- Opted-out roasters are excluded.
- Roasters known only from OpenStreetMap are excluded.

`coffeedata export-public --to <dest>` writes them as Parquet. With `PUBLIC_EXPORT_URL` set, the weekly run does
it automatically. A directory or Hugging Face export also writes `manifest.json`: the release (`release_build_id`,
`release_run_date`), the contract id and version, the data licence (`CDLA-Permissive-2.0`,
[`licenses/CDLA-Permissive-2.0.txt`](../licenses/CDLA-Permissive-2.0.txt)) and each Parquet file's `sha256` and row
count, with the licence text beside it as `LICENSE`. An `s3://` or `gs://` export writes only the Parquet files. A Hugging Face export of a release build tags its commit `release-<run date>`
(the Monday run; a newer build of the same run moves the tag to its commit), so consumers can pin a week:
`hf://datasets/<you>/us-coffee-market@release-2026-11-02/data/public_coffee.parquet`.

| Destination | `--to` / `PUBLIC_EXPORT_URL` | Credentials |
|---|---|---|
| Public Hugging Face dataset | `hf://datasets/<you>/us-coffee-market` | `HF_TOKEN` (write) |
| Snowflake (direct) | `coffeedata load-snowflake` (internal stage, no S3) | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY` |
| Databricks (via S3) | `s3://<bucket>/coffee-data/` | AWS credentials (env, profile or role) |
| … via GCS | `gs://<bucket>/coffee-data/` | `GCS_HMAC_KEY_ID`, `GCS_HMAC_SECRET` |

Snowflake loads straight from the weekly job, in its own step after the site is built (so a Snowflake problem
never holds the site back):
- The job uploads the export of the served release to an internal stage, one folder per run, never overwritten.
- It checks every table's and share view's columns against the export before loading any. If the public
  contract changed (`contracts/public-catalog.odcs.yaml`, a new version, even one that only adds a column),
  it loads nothing and the step fails. **Every scheduled run then stays red on this step until someone
  migrates**; the site and the Hugging Face export still publish meanwhile. To migrate, run the weekly workflow by hand with only `snowflake_migrate`
  checked: that doesn't crawl, it re-exports the served release (`coffeedata rollback --to served`) and runs
  `coffeedata load-snowflake --migrate`. Locally: `coffeedata rollback --to served`, then
  `coffeedata load-snowflake --migrate`.
- A migration first checks, before changing anything, that the loader's role (COFFEE_LOADER) owns the share
  views, from the views' own grants (`show grants on view`, which needs no account privilege). It uploads the
  files, drops and reloads the changed tables, then recreates every share view with `copy grants`, so each
  stays in the share (`SNOWFLAKE_SHARE`, default `COFFEE_DATA_SHARE`). Only a view missing from the share is
  granted, first, and that one grant needs the share's owner. A migration that fails partway is finished by running it again; until then the weekly
  load refuses to run.
- Migrate as COFFEE_LOADER, not ACCOUNTADMIN: a role that recreates a table or view owns it afterwards, and the
  weekly load and later migrations, run as COFFEE_LOADER, would then fail. If that happened, give each one back
  as ACCOUNTADMIN with `grant ownership on view coffee_data.share.<view> to role coffee_loader copy current
  grants` (and `grant ownership on table coffee_data.market.<table> to role coffee_loader copy current grants`).
- Before the first real migration, run `coffeedata load-snowflake --migrate --dry-run`: it changes nothing and
  prints what the checks read (tables, share views, owners, grants), whether it would refuse, and every
  statement it would run.
- It replaces each table in one transaction.
- On every load, a migration included, it then writes each table's, share view's and column's description
  from the public contract, read at load time, so **the listing's data dictionary comes from
  `contracts/public-catalog.odcs.yaml`**: `comment on table` / `comment on view`, and `alter table|view ... alter
  column ... comment` for columns (Snowflake refuses `comment on column` with a qualified name). A migration
  writes the view's descriptions into its definition (`create or replace secure view ... (col comment '...', ...)
  copy grants comment = '...' as select ...`), so recreating a view can't drop them. A column in the export that
  the contract doesn't describe fails the load before anything changes; a contract column Snowflake doesn't have
  yet is skipped with a warning. To change a description, change the contract; the next load applies it.
- It fails if the row counts differ from the export.
- The tables in `MARKET` keep the export's lower-case column names (quoted identifiers, as `infer_schema` makes
  them). The share views rename them to upper case (`create or replace secure view SHARE.COFFEE (RELEASE_RUN_DATE
  comment '...', ...) copy grants comment = '...' as select "release_run_date", ... from MARKET.public_coffee`),
  so consumers write `RELEASE_RUN_DATE` (or `release_run_date`) without quotes. Columns are compared in any case.
  A share view with the export's columns that isn't upper case yet (made by `admin_setup.sql` part 2, or before
  this change) is recreated by any load; no `--migrate` is needed. Unlike a migration, an ordinary load never
  stops for it: if another role owns the view, the share is missing it and COFFEE_LOADER can't grant it, or its
  grants can't be read, the load leaves that view lower case, loads the data, and warns (on stderr and as a
  warning on the workflow run's summary page) with the statement that fixes it. After recreating a view, the load
  reads its grants back and fails the job if the view is no longer in the share. To re-case before the Tuesday
  `site` run, `gh workflow run weekly -f snowflake_migrate=true` works too, but it is the strict path: it stops,
  loading nothing, if any view can't be recreated. Run `coffeedata load-snowflake --migrate --dry-run` first to
  see what it would refuse; a dry run only records the `grant … to share` statements, so a share grant that would
  fail shows only in the real run.

Databricks loads from S3 or GCS.

## Coverage claims

`LISTING.md` is the source of truth for the listing copy. The Snowflake listing's attribute boxes are typed by
hand from it, so nothing else would notice when a number there goes stale. Its `coverage-claims` comment maps
each number in the copy to a `mart_build_profile` metric (a `listed_coffees: <the copy's words>` line), and
every build compares them. `listed_coffees` counts the coffees currently listed for sale, not
wholesale (not `PUBLIC_COFFEE`'s rows), and `roasters_with_coffee` the roasters with at least one of them:
- `coverage_claims_match_build` warns when a claim is more than 15% (`claim_tolerance` in
  `dbt/dbt_project.yml`) from this build's metric, and `coffeedata build` prints the file and line to update
  and the build's number rounded for the copy ("about 41,000 now"). It never holds a release back. Update the copy and the comment together, then retype the number in Snowflake.
- `coverage_claims_name_metrics` fails the build when a claim names a metric the profile doesn't have: that's a
  bug in the claim or the profile, not drift.
- A claim whose text isn't in the copy, or has no number, fails CI
  (`test_the_listings_coverage_claims_parse_and_point_at_their_copy`). In a build it's logged and skipped, and
  a missing `LISTING.md` is logged and checks nothing: the ingest image copies the file, but the check never
  stops a build.

Both results are in the build's record (`builds/`, like every other check) and in the job log.

## Order of operations

1. Hugging Face. Publish to a separate public dataset, so the main lake stays private. It's free and shows
   whether anyone uses the data.
2. Snowflake Marketplace. Run part 1 of `snowflake/admin_setup.sql` (service user), set the three
   `SNOWFLAKE_*` values, run `coffeedata load-snowflake`, then part 2 (share). A free listing needs a full account, a provider
   profile and acceptance of the Provider and Consumer Terms, and Snowflake reviews it before it goes live.
   Running costs are an XS warehouse for a few seconds a week, plus storage.
3. Databricks Marketplace. Run `databricks/setup.sql`. This needs a Premium workspace with Unity
   Catalog. Public listings go through the Data Partner Program application; private exchanges are self-serve.

## Before going public

- License: the public data is CDLA-Permissive-2.0
  ([`licenses/CDLA-Permissive-2.0.txt`](../licenses/CDLA-Permissive-2.0.txt)), matching Overture; the code is MIT. Use the same licence in every listing.
- Terms of service: some roasters' terms restrict automated collection or commercial reuse. A free
  listing of facts with links back and an opt-out is the safer option. Get legal advice before charging.
- Support contact: put a support email in `LISTING.md`.
