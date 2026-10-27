# Publishing the public tables

The weekly run builds four **facts-only** dbt models (`dbt/models/public/`). The `public_tables_are_facts_only`
dbt test fails the build if any of them ever exposes descriptions, images, store ids or shot-log columns.

- Product descriptions, images, handles, tags and the shot log are excluded.
- Opted-out roasters are excluded.
- Roasters known only from OpenStreetMap are excluded, so ODbL share-alike doesn't apply.

`coffeedata export-public --to <dest>` writes them as Parquet. With `PUBLIC_EXPORT_URL` set, the weekly run does
it automatically.

| Destination | `--to` / `PUBLIC_EXPORT_URL` | Credentials |
|---|---|---|
| Public Hugging Face dataset | `hf://datasets/<you>/us-coffee-market` | `HF_TOKEN` (write) |
| Snowflake / Databricks (via S3) | `s3://<bucket>/coffee-data/` | AWS credentials (env, profile or role) |
| … via GCS | `gs://<bucket>/coffee-data/` | `GCS_HMAC_KEY_ID`, `GCS_HMAC_SECRET` |

Snowflake can't read `hf://`, so both marketplaces load from S3 or GCS.

## Order of operations

1. **Hugging Face, public.** Publish to a *separate* public dataset (your main lake stays private). This
   costs nothing and shows whether anyone uses the data.
2. **Snowflake Marketplace.** Run `snowflake/setup.sql`. A free listing needs a full account, a provider
   profile and acceptance of the Provider and Consumer Terms, and Snowflake reviews it before it goes live.
   Running costs are an XS warehouse for a few seconds a week, plus storage.
3. **Databricks Marketplace.** Run `databricks/setup.sql`. This needs a Premium workspace with Unity
   Catalog. Public listings go through the Data Partner Program application; private exchanges are self-serve.

## Before going public

- **License.** The dataset card proposes CDLA-Permissive-2.0, matching Overture. Change it if you prefer
  CC BY 4.0.
- **Terms of service.** Some roasters' terms restrict automated collection or commercial reuse. A free
  listing of facts with links back and an opt-out is the conservative path; get legal advice before
  charging for it.
- **Support contact.** Put a support email in `LISTING.md`.
