# Publishing the public tables

The weekly run builds four facts-only dbt models (`dbt/models/public/`). The `public_tables_are_facts_only`
dbt test fails the build if any of them ever exposes descriptions, images, store ids or shot-log columns.

- Product descriptions, images, handles, tags and the shot log are excluded.
- Opted-out roasters are excluded.
- Roasters known only from OpenStreetMap are excluded, so ODbL share-alike doesn't apply.

`coffeedata export-public --to <dest>` writes them as Parquet. With `PUBLIC_EXPORT_URL` set, the weekly run does
it automatically.

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
  contract changed (`contracts/public-catalog.odcs.yaml`, a new major version), it loads nothing and the step
  fails. **Every scheduled run then stays red on this step until someone migrates**; the site and the Hugging
  Face export still publish meanwhile. To migrate, run the weekly workflow by hand with only `snowflake_migrate`
  checked: that doesn't crawl, it re-exports the served release (`coffeedata rollback --to served`) and runs
  `coffeedata load-snowflake --migrate`. Locally: `coffeedata rollback --to served`, then
  `coffeedata load-snowflake --migrate`.
- **Before the first real migration, do a dry run:** run the weekly workflow with `snowflake_migrate` and
  `snowflake_dry_run` checked (locally: `coffeedata load-snowflake --migrate --dry-run`). It changes nothing in
  Snowflake (the workflow re-serves the same release to export it, recorded as a Snowflake dry run), and prints the role, the share, the tables whose columns changed, each share view's state (ok, stale, new or
  hidden), the grants it would add, every reason it would refuse, and the statements it would run.
- Every load checks first, with read-only queries, and changes nothing if a check fails:
  - Snowflake shows a role only the objects it owns or was granted, so a table or view another role owns
    (one recreated as ACCOUNTADMIN) is invisible to COFFEE_LOADER. A migration reads which views are in the
    share from the share's own grants (`show grants to share`; COFFEE_LOADER created the share in part 2 and
    can always list it), so a view that's in the share but invisible stops it, with the `grant ownership`
    command to give it back. A table or view another role owns stops it the same way.
  - A view that doesn't exist and isn't in the share is new (a public table added since part 2): the migration
    creates it and grants it to the share.
  - Every table is created before any is dropped or truncated, so a table that exists but is invisible fails
    before anything is loaded.
- A migration uploads the files, drops and reloads the changed tables, then recreates every share view with
  `copy grants`, so each stays in the share (`SNOWFLAKE_SHARE`, default `COFFEE_DATA_SHARE`). A view missing
  from the share is granted first. A migration that fails partway is finished by running it again; until then
  the weekly load refuses to run.
- Migrate as COFFEE_LOADER, not ACCOUNTADMIN: a role that recreates a table or view owns it afterwards, and the
  weekly load and later migrations, run as COFFEE_LOADER, would then stop. If that happened, give each one back
  as ACCOUNTADMIN with `grant ownership on view coffee_data.share.<view> to role coffee_loader copy current
  grants` (and `grant ownership on table coffee_data.market.<table> to role coffee_loader copy current grants`).
- It replaces each table in one transaction.
- It fails if the row counts differ from the export.

Databricks loads from S3 or GCS.

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

- License: the dataset card proposes CDLA-Permissive-2.0, matching Overture. Change it if you prefer
  CC BY 4.0.
- Terms of service: some roasters' terms restrict automated collection or commercial reuse. A free
  listing of facts with links back and an opt-out is the safer option. Get legal advice before charging.
- Support contact: put a support email in `LISTING.md`.
