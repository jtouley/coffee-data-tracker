# admin.registry.coffee

The owner's command center, behind passkey sign-in: what the pipeline wrote to the lake, and the actions that
replace the CLI. It implements `docs/design/admin-registry.md` (#112): review the build's flags, start any pipeline
job off-cycle, include or exclude a coffee, change the rule settings and a store's crawl flag, build those changes as
a draft, Publish it, Roll back. The admin reads the lake and never writes
it: the lake changes only through the pipeline's own Cloud Run jobs, which the admin starts. The pages' design is in
[DESIGN.md](DESIGN.md).

## Pages

| Page | Answers | Actions | Reads |
|---|---|---|---|
| Week | Did the week land (each connector's passes, merge, enrich, classify, build, the served release), and what failed (by HTTP status, challenge, partial, quarantined; the trend; every failed store) | a Test crawl link per failed store | `job_manifests`, `fetches`, `builds`, `serving_log`, ops `run_completeness` |
| Queue | The served build's open flags by product, sorted by the public rows a removal touches, products with the same evidence together, one line each ("2 supports · ready", "1 doubt · 1 restore"); a product's case (`?case=<coffee_id>`): each flag with its polarity and evidence, the classifier's answer, the listing (type, tags, sizes, prices, description and how many products share it), the store's last 8 runs of fetches, its crawl flag and live check; the rule settings | Exclude or Include one product, or a group with the same evidence; Live check (a test crawl); the store's crawl flag; edit the rule settings' params | ops `mart_flags`, `fetches`, the control plane |
| Filters | The owner's keyword filters: one line (filters · products excluded), each filter with where it's looked for, how many of the served build's products it matches and why; recent changes | Add filter (a sheet); Remove (each row's ⋯); Undo; each change previewed against the served release before it can be saved | the control plane, ops `product_decisions` |
| Jobs | Every job started from here, with its live status, duration, logs link and who started it | start any pipeline job; cancel a running one | the control plane, Cloud Run |
| Release | The served release; the decisions not in it; drafts and their state; a build's changes (`release_changes`) and gate | Build a draft, Publish, Roll back, Discard | `builds`, `serving_log`, ops `release_changes`, the control plane |
| Catalog | Every product of the served build (or one Release links to), coffee or not, with search and facets, and the owner's decision | Tick products, then Include or Exclude them, by `coffee_id` or by version (`input_hash`), with a reason in the confirm step; Undo | ops `product_decisions`, the release's `dim_coffee`, the control plane |
| Sources | Every place discovery has listed (lake `candidates`: roastery-tagged places, and cafés and coffee shops with a storefront website) and every roaster in the registry, one row per domain, with its status (Roaster (inherited), Roaster, Not a roaster, Unreviewed), whether a registry roaster is a chain, its hints (tag, "roast" in the name), and what the last discovery run added or dropped | Search (a pasted address finds its domain); one Filter / Sort panel (status, chain, origin, tag, state, city); on a desktop a table whose headers sort by Name, Domain, City, State, Tag, Hints, Status or First seen (on a phone, the sheet's sort options); select rows (box or tap) and flag them Roaster or Not a roaster from the action bar, or one place from its ⋯ menu (also Clear the flag, Open site); a registry roaster's chain flag, with a reason, from its ⋯ menu; Undo | the lake's `candidates`, the served release's `dim_roaster`, the control plane |
| Log | Every write: who, when, what, the outcome | download the decisions (for `coffeedata decide export-labels`) | the control plane |

The Catalog copies a build's two files into a local DuckDB file once (releases are immutable) and filters there.
The lake is opened with `Lake(read_only=True)`: nothing is created, every write method raises `ReadOnlyLake`, and a
remote lake is read directly, never through a staging folder.

## Jobs

Every job is one entry in `admin/jobs.py` (`ACTIONS`): its fields and the Cloud Run job and arguments it starts.
The admin starts them with the Cloud Run Admin API's run-with-overrides, as `gcloud run jobs execute --args` did:

| Action | Job and arguments |
|---|---|
| Crawl (one connector, or all five: a full crawl) | `crawl-<connector>`: `crawl --connector <c>` + the mode |
| Crawl one roaster (`--only`; in test mode, the live check of a store) | `crawl-<connector>`: `crawl --connector <c> --only <roaster>` + the mode |
| Shopify retry | `crawl-retry`: `crawl-retry --connector shopify` + the mode |
| Fetch health | `fetch-health --run-date <week> --force` |
| Merge | `merge --run-date <week>` |
| Enrich | `enrich-week --submit --run-date <week>` |
| Classify | the `build` job: `coffeedata classify`, then `publish` |
| Build | the `build` job with its own arguments (it serves the release if it passes) |
| Draft build, Publish, Roll back | from Release: `build --draft … --snapshot …`; the `publish` job's `publish-release` |

**Modes.** *test* (the default) writes to the week's `test=<id>` sub-partition, which the merge never reads, and its
`job_manifests` rows say arm `test`, so it never counts as the week's crawl. *count* writes into week W's
partition like the scheduled passes; it is refused once W's merge is done.

**Guards.** A job with a running execution isn't started again. Starts are refused from Monday 09:00 UTC until the
week's merge is done and on Tuesday 03:00-12:00 UTC, unless the owner gives a reason, which is recorded. One test
crawl per roaster a day. The site isn't a job here: it deploys from the served release by the `site` workflow on
GitHub, which the admin holds no token for (Jobs links to it).

Adding a job is one `Action` in `admin/jobs.py` and its name in `local.admin_jobs` (`src/terraform/admin_control.tf`);
`tests/test_admin_terraform.py` fails if the two differ.

## Decisions, drafts and Publish

- A **decision** is an event in the control plane: `include` or `exclude`, for a coffee (`coffee_id`) or a product
  version (`input_hash`, every product sharing it), with a reason. A coffee's own decision wins over its version's.
  Undo appends a `reverted` event. Several products decided together are a group, undone together.
- **Build a draft:** the admin writes the effective decisions as an immutable snapshot to its snapshot bucket, then
  starts the build job with `build --draft <id> --snapshot <id> --snapshot-sha256 <digest>`. The build checks the
  digest (a changed or missing snapshot fails it), applies the decisions in dbt (`stg_decisions`, `decided()`,
  the `decisions_applied` test), records the build with its draft id and snapshot, and never serves it.
- **Publish** is off until:
  - the draft build passed;
  - the served release is still the one it was built against;
  - CLAUDE.md step 9's running total stays at 50 or fewer: the public coffees the published drafts' decisions changed
    since the last recorded full evaluation, plus this draft's (Release records an evaluation, which starts the total
    again);
  - a rule settings change to how a rule decides has its evaluation (`coffeedata.rules.EVALUATED`);
  - a keyword filter change: every public coffee it moves counts toward the running total. A removal never waits.
    A new filter over 50 waits until every product it takes out that Claude calls coffee or hasn't seen is marked
    Keep excluded (none Drop the filter); Classify these asks Claude about the unseen ones first
    (eval/prefilter/README.md);
  - the owner typed the count, when more than 50 public rows leave;
  - each store this draft removes a coffee from (a new exclude, an undone include, every roaster of an excluded
    version) answered a test crawl of the store itself in the last 7 days (a connector-wide test doesn't say which
    stores answered).

  It starts the `publish` job (`publish-release --action publish`). "Published" is read from `serving_log`, never
  set. A draft being published, or published, can't be discarded.
- **Roll back** serves a release that was served before (`publish-release --action rollback`); an unpublished draft
  can't be rolled back to.
- **The weekly build** applies the served build's snapshot, which its builds row carries, so it needs no bucket and
  no database.
- **Labels:** Log's download is the effective decisions as a snapshot, with each version's coffees listed; `coffeedata decide export-labels --snapshot
  decisions.json` turns them into candidate labels for the classifier's train set only. A coffee in the sealed test
  set or the validation set is left out and only counted (CLAUDE.md: an owner correction is one labeler).

## Sources

Discovery (`coffeedata discover overture`) reads Overture's coffee places. Roastery-tagged places go straight into
the registry, as before. Every place it reads (the roasteries, plus `coffee_shop` and `cafe` places whose website
passes `registry.is_storefront_host`) is also recorded in the lake's `candidates` table, without any request to the
place's site.

The Sources page lists those places together with the served release's registry (the curated seed, the issue forms
and discovery's roasteries). It copies them into a local DuckDB file once per discovery run and served build, the
way the Catalog does, and searches and pages there (100 rows a page; about 47,000 places).

- **Every registry roaster inherits Roaster.** With no flag set, the registry, the probe and the crawl are exactly
  what they were (`tests/test_sources.py::test_the_registry_and_the_crawl_are_unchanged_until_flagged`).
- **Flags** are written to `control.source_flags` (append-only). Each write is CSRF-checked, idempotent on its key,
  audited in the same transaction, refused with 409 if another tab flagged the place since the page loaded, and
  undoable as a batch.
- **Flags reach the crawl through the snapshot.** The next draft carries `sources` (listed places flagged Roaster,
  not already in the registry) and `not_roasters` (registry roasters flagged Not a roaster). Once that draft is
  published, `registry.current` adds the flagged places from `candidates` and drops the not-roasters, so the probe
  and the weekly crawl pick them up or leave them. dbt's `stg_registry` adds the same places through the var
  `source_roasters`. A newly flagged roaster is probed at the next probe run and crawled once a probe finds its
  platform. Unreviewed places are never probed or crawled. A snapshot that can't be read applies no flags.
- **Not a roaster leaves the crawl, the build and the catalog.** The next draft's snapshot carries it in
  `not_roasters`: `registry.current` drops it from the probe and the crawl, and dbt `stg_registry` (var
  `not_roasters`) drops it from `dim_roaster` and the public roaster table. In the same write it records an exclude
  decision for each of the store's coffees in the served release (grouped under the flag's batch). Undo restores the
  flag and the coffees; flagging it Roaster or clearing the flag reverts the exclusions, and undoing that excludes
  them again.
- **Pull now.** The action bar (for the selected places) and a Roaster place's ⋯ menu offer Pull now. It is one
  Cloud Run job, `pull` (`coffeedata pull --only <domains> --run-date <week>`, then a publish of its probe rows):
  it probes the places, then crawls each one that answered with a storefront, counted, on its platform's connector,
  in the same execution. Nothing waits for a page to be open, and no GET starts anything. The week is this week
  until its merge is done, then next week: a merged week's partition is never read again, and next week's merge and
  build keep the data (that place counts as done for next week's crawl). It runs on any day, with the same guards
  (overlap, and the Monday and Tuesday windows with the owner's override) and the same audit, and Jobs shows it.
  No Publish is needed: `--only` crawls a place of the source list before it is in a published registry
  (`ingest.select_states`); the next published draft carries the flag, so the weekly crawl keeps it. Only places
  flagged Roaster can be pulled; a place whose probe finds no storefront is not crawled. The job leaves from the
  WooCommerce and generic NAT IP (`src/terraform/ingestion.tf`).
- **Fails closed.** If the served release's `dim_roaster` can't be read while there are flags, Release says so and
  refuses a draft, rather than treating the registry as empty. If the flags can't be read (control plane down),
  Sources still lists the places but shows no status and no flag counts.
- **Chain flags.** The owner's decision: chains are still crawled, but left out of the public data, and a chain is
  anything with more than 20 stores (dbt `int_roaster_state.is_chain`, var `chain_store_threshold`; the store count
  is the registry's `locations`). A registry roaster's ⋯ menu says whether it is a chain and why (its count, or the
  flag and its reason), and offers the way to the other status: Mark as a chain, Not a chain, or Clear the chain
  flag where clearing gives the other status (the count decides again). Each asks a reason in the confirm sheet. The
  flag is written to `control.chain_flags` (append-only; migration `0006_chains.sql` starts it with the four rows of
  `dbt/seeds/chain_overrides.csv`), CSRF-checked, idempotent on its key, audited (`chain_flag`) in the same
  transaction, and refused with 409 if another tab changed the roaster's flag since the page loaded. The next draft's
  snapshot carries every flag in effect as `chains` ([{roaster_id, chain}], even when empty), and `coffeedata build`
  passes it to dbt as var `chain_flags`, which then replaces the seed (#146). A snapshot from before chain flags
  carries none, and dbt keeps reading the seed. Release counts the chain flags not in the served release, so the
  first draft after this ships carries the four seed rows. Without the control plane, Sources shows no chain word,
  no chain count and no Chain filter.
- **Hints never flag anything.** The hints are the Overture tag and "roast" in the name. The evidence from the
  screening experiment (#130) is not shown: it is not in the lake, and the admin reads only the lake.

## Rules and flags

Code defines the rule types (`coffeedata.rules.RULE_TYPES`); the admin's settings configure them, as versions in
the control plane, checked against the deployed code before they are saved. Each draft's snapshot carries the
settings and the crawl flags, so builds read them without the database; a build without a snapshot uses the
defaults.

- **Flags** are computed in the build (dbt `flag_<rule>`, collected in the ops table `mart_flags`), never in the
  admin. Each has a polarity: `supports` a removal, `doubts` it, or argues to `restore` the product.
  - `verdict_flip` (classifier): the classifier's answer differs from the served release. `removed` (it was public)
    is `restore`; `added` (seen before, now coffee) is `supports`.
  - `shared_body` (catalog, `doubts`): the description, or a paragraph of 60 characters or more, is on 3 or more of
    the store's products: pasted text.
  - `non_coffee_type` (listing, `supports`, proposes exclude): the store's product type names a kind that isn't
    coffee (drink mix, nuts, books, parts).
- **Readiness.** A removal is ready only with `supports` from two distinct families, no open doubt and no coverage
  problem at the store (dbt `int_catalog_decided.ready_flags`, tested by `ready_removals_have_no_doubts`), and an
  answered live check of the store (Publish).
- **Group approval.** A group is one store's products with the same flags' evidence (`evidence_hash`, which includes
  the store). The Queue offers one decision for the group, and that decision records the evidence; the store's one
  live check covers it. A live check never speaks for another store, so evidence shared across stores (a verdict
  label, a product type) doesn't make a group.
- **Flags change nothing public.** A removal is the owner's decision in a draft. A guard that kept doubted classifier
  removals public was evaluated and cut: 4 of the 150 products it held on the sealed test were coffee
  (`eval/guard/RESULTS.md`).
- **Settings edits.** A change to how a rule decides (a family, a polarity or proposal, readiness)
  needs its full evaluation before Publish whatever it counts: Publish stays off until `coffeedata.rules.EVALUATED`
  names that exact value, which lands with the evaluation on `main`; returning to the defaults never needs one. A
  param change only changes which products are flagged.
- **The crawl flag.** Off stops scheduled crawls of the store (`ingest.select_states`, crawl-retry's parked stores
  and the fetch-health re-fetch) once a draft carrying it is published; a live check still runs.
- **Adding a rule** is one `RuleType` entry in `coffeedata.rules`, its model `dbt/models/intermediate/flags/
  flag_<id>.sql` and its `schema.yml` entry, and its default in `dbt_project.yml`'s `rules` var (a test checks the
  var matches the code). The settings schema, the Queue, the Case and the readiness come from the entry.

## The control plane

Postgres (`admin/controlplane.py`, migrations in `admin/migrations/*.sql`): the audit trail, decisions as events,
job runs, snapshots, drafts, rule settings versions, keyword filters (as events) and the owner's review of what a
filter change excludes, crawl flags, chain flags and idempotency keys. Every table is append-only (a trigger refuses UPDATE, DELETE and
TRUNCATE). The admin applies its migrations on its first use of the database after it starts (the first page or write
that needs it), once each, under an advisory lock, in a `control` schema it creates and owns. They are additive and
forward-only, so rolling the admin's image back to an earlier one leaves a schema it can still use.

Hosted, it is Cloud SQL for PostgreSQL 16 (`admin_control.tf`), reached through the Cloud SQL Python Connector with
IAM database authentication: the admin's service account is the database user and there is no password anywhere.
Every write is recorded in the same transaction as its effect and fails closed: if the control plane can't record a
write, nothing is started, decided or published (503).

## Sign-in

Passkeys only (WebAuthn, the `webauthn` package), no password:
- **Enrollment, once.** While no passkey is registered, `/enroll` takes the `ADMIN_ENROLL_TOKEN` secret and
  registers the first passkey. It shows ten recovery codes, once. That registration closes enrollment for good,
  whatever the secret still holds.
- **The second passkey** is added from Account within 10 minutes of signing in, which is the enrollment session in
  practice. After two, another passkey needs a recovery code.
- **Recovery:** a recovery code registers a new passkey, retires every earlier passkey (recovery means they are
  lost) and signs out every session. Each code works once. Then add the second passkey again from Account within 10
  minutes. Lost only one passkey? Recover from a device you control: it retires both, then add the other again.
  New recovery codes come with the write API; Account shows how many are left.
- **Sessions** last 12 hours from sign-in and never renew. Sign out revokes the session on the server; "Sign out
  everywhere" revokes them all.
- **Limits:** failed tokens, codes and passkeys are counted per address and overall, and once over the limit
  nothing more is checked for 15 minutes.
- **Every route but `/health`, the sign-in pages and their API, and static files needs a session**, checked before
  any route runs, so a new route is protected by default. A missing or weak setting switches the whole service off
  (503), and it never falls back to showing data. No setting or environment variable turns sign-in off.

The sign-in state is kept as one JSON record per change, each created once and never replaced: in a Cloud Storage
bucket when hosted, in a local folder on a laptop. It is not lake data.

## Settings

| Variable | What | Hosted |
|---|---|---|
| `ADMIN_ORIGIN` | the admin's origin, e.g. `https://admin.registry.coffee`; the passkeys' relying party | required, https |
| `ADMIN_SESSION_SECRET` | 32+ random characters; signs the session cookie | required (Secret Manager) |
| `ADMIN_ENROLL_TOKEN` | 32+ random characters; enrolls the first passkey, once | required until enrolled (Secret Manager) |
| `ADMIN_STATE_BUCKET` | the bucket that holds the sign-in state | required |
| `ADMIN_STATE_DIR` | a local folder instead, on a laptop only | refused |
| `HF_TOKEN` | a fine-grained Hugging Face token with read access to the lake dataset only (secret `ADMIN_HF_TOKEN`) | required |
| `COFFEEDATA_LAKE` | the lake (the image's default is the production dataset) | |
| `ADMIN_CACHE_DIR` | where the Catalog's per-build files go | `/tmp/admin-cache` |
| `ADMIN_DB_INSTANCE`, `ADMIN_DB_USER`, `ADMIN_DB_NAME` | the control plane: Cloud SQL connection name, the IAM database user, the database | set by Terraform |
| `ADMIN_DATABASE_URL` | a local Postgres instead (`postgresql://user@host:port/db`), on a laptop only | refused |
| `GOOGLE_CLOUD_PROJECT`, `ADMIN_RUN_REGION` | where the pipeline's Cloud Run jobs are | set by Terraform |
| `ADMIN_SNAPSHOT_BUCKET` | where drafts' snapshots go (the build job mounts it read-only) | set by Terraform |
| `ADMIN_SNAPSHOT_DIR` | a local folder instead, on a laptop only | refused |

Without the database, Cloud Run or the snapshot store, the pages still read the lake and every action says why it is
off.

## Running it locally

```sh
export ADMIN_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ADMIN_ENROLL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
make admin   # http://localhost:8001 over ./lake (COFFEEDATA_LAKE), state in build/admin-state
```

Open `http://localhost:8001` (passkeys need `localhost` or https), enroll with the token, and save the recovery
codes. Delete `build/admin-state` to start over.

## Tests

- `tests/test_admin_actions.py`: every write against a local Postgres (`tests/pg.py` starts one; `REQUIRE_POSTGRES=1`
  makes its absence a failure) and a fake Cloud Run (`tests/admin_fakes.py`): the guards, idempotency, the audit,
  CSRF and sign-in, fail-closed, decisions, drafts, Publish's checks, Roll back. `tests/test_drafts.py`: draft builds
  over a snapshot through dbt, publish-release, export-labels. `src/terraform/tests/admin_grants.tftest.hcl`: the
  admin's grants (`terraform test`, a mock provider).
- `tests/test_admin.py`: every query on a small synthetic lake, the Catalog's cache and facets, sign-in end to end
  with a software authenticator, the state store, the settings, a walk over every route signed out (only the
  public ones answer), and that no page writes to the lake.
- `tests/test_admin_browser.py` (Node with Playwright, and dbt): builds a three-week lake through
  `coffeedata build` (`tests/admin_fixture.py`), then drives every page and state with WebAuthn virtual
  authenticators, at 390 px and 1,280 px, light and dark, with axe. `ADMIN_SCREENSHOTS=<dir>` keeps the
  screenshots.

## Deploying

Terraform: `src/terraform/admin.tf` (the service, its own service account, the three empty secrets, the state
bucket, the domain mapping, and alerts on errors, passkey changes and the cold start) and `admin_control.tf` (the
Cloud SQL instance, its IAM database user, the snapshot bucket, the job grants and an alert on every write). The
owner applies a reviewed plan.

**Once, after the first apply:** the admin's IAM database user needs the right to create its schema in the `admin`
database. As the `postgres` user (Cloud SQL Studio, or `gcloud sql connect registry-coffee-admin --user=postgres
--database=admin` after setting that user's password in the console):

```sql
GRANT CREATE ON DATABASE admin TO "registry-coffee-admin-run@<project>.iam";
```

(`terraform output admin_db_user` prints the name.) The admin then creates the `control` schema and its tables at
its next start, and owns them. Nothing else is granted to it.
