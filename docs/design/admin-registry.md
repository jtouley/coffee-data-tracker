# admin.registry.coffee: a control plane and data catalog

Status: proposal, revised 2026-11-06 with the owner's decisions on #112. Nothing here is built. Owner: jtouley.

**What the owner asked for.** Flags about the data, raised automatically and backed by metadata, are reviewed in one
place before anything is removed ("Before we remove things, they need flagging [from] multiple issues").
"It builds a dbt version draft that checks the outcomes, but doesn't publish unless I allow." "CLI and APIs should
all be built as part of admin.registry.coffee." Rules and excludes are "config-driven, code-backed, from the admin
panel".

**The design in six lines.**
1. **The control plane is a database, and it is also the data catalog:**
   - rule settings and their versions;
   - per-item decisions and flags, with evidence;
   - roaster state (the crawl flag, coverage, blocked weeks);
   - data-quality results per build;
   - table and contract metadata.
2. **Code defines and the database configures.** Rule types and their settings schema live in git, and the schema is
   generated from the code. Every change is a new version row; nothing is updated in place.
3. **Builds never read the live database.** A draft build exports an immutable snapshot of the settings and
   decisions to the private lake and pins its id, so every rebuild is exact.
4. **Every admin change runs a draft build.** It is the normal build with every gate, recorded but not served. It
   shows the real diff against the served release.
5. **Publish is always the owner's explicit action.** Stale drafts rebuild and re-test themselves but never publish.
   The weekly release stays automatic, gated by the tests.
6. **The admin's API is the only writer.** The UI and `coffeedata decide` are its clients. Sign-in is passkey-only.

## 1. Today (checked on `origin/main`, 8b54f80)

| Piece | Where | What it means here |
|---|---|---|
| Build | `cli.cmd_build` | dbt builds and tests; `quality.record_build` writes `builds/` and an immutable `releases/…/build=<id>/` for every build, passing or not; `quality.serve` runs only if it passed |
| Serve, rollback | `quality.serve`, `quality.rollback(to=…)`, `cmd_rollback` | serve any recorded passing build, log it in `serving_log`, then export the public tables and the site |
| Diff | `quality.build_diff`, `coffeedata build-diff` | models, tests, counts, metrics, and the coffees added, delisted or out of stock |
| Release notes | #111 (open) `release_changes` | every public row in or out against the served release, with a reason |
| Item overrides | lake `run_overrides/` (roaster-week `accept` / `reject` / `use_batch`) | the only kind. No product or variant override, no "sells no coffee" status, no crawl flag |
| Rules | `classify.py`, `products.decide`; thresholds in code and dbt vars | `dim_coffee` drops a product as soon as its latest version isn't coffee. That is how the 16 Stivers coffees went |
| Verdict guard | #108 (merged): `classify` fails loudly; `verdicts_cover_catalog` | verdicts now run in production |

So a draft needs **one new flag on an existing command**: `coffeedata build --draft <id> --snapshot <id>` records the
build and skips `serve`. Publish is `serve` on that build. There is no separate replay system.

## 2. The database

### 2.1 Why a database, and not git (this overrules the platform review)

The platform review (issuecomment-6641959444) recommended rule settings in git and decisions in an HF dataset.
The owner chose a database, because **settings edited in the admin panel need one behind them**: git can't be their
home if the panel is where they change. Here is how each of the review's concerns is kept:

| The review's concern | How it is kept |
|---|---|
| Exact rebuilds (one SHA pins code and params) | Builds read only an immutable **snapshot** exported to the lake, never the live database. The build records the snapshot id, the git SHA and the image digest |
| Audit and review | Every change is a new version row: who, when, why, the old value and the new one. The draft build is the review: its gates and diff |
| Config and image skew | The settings schema is generated from the code's rule types. The API rejects a value the deployed schema doesn't allow, and a draft build fails on a snapshot its image can't read |
| A database down breaks the weekly build | It can't: the weekly build reads the **last published snapshot** from the lake. A database outage only blocks admin edits |
| A password in Secret Manager and state | Passwordless database auth is a POC success criterion (section 7) |
| Back up off one store | The database has point-in-time restore. Every published snapshot is in the lake, and each build's release folder keeps its effective decisions |
| Fail closed | A build whose pinned snapshot can't be read fails; it never builds as if there were no decisions |

### 2.2 Tables

Every table is append-only. The "current" value is the latest version row.

| Table | One row per | Holds |
|---|---|---|
| `rule_settings` | settings version | the whole YAML-shaped document below, its schema version, who, when, why, and the previous version id |
| `decisions` | decision event | subject (product `coffee_id`, variant, roaster, batch), action, event (`checked`, `proposed`, `approved`, `discarded`, `reverted`), `draft_id`, flag ids, frozen evidence, live check, actor, client (`ui` / `cli`), reason |
| `roaster_state` | roaster state version | `crawl` (on/off), coverage notes, blocked weeks (#110), who, when, why |
| `flags` | flag × build | imported from each build: rule, polarity, evidence (references only), `evidence_hash`, public rows affected |
| `dq_results` | test × build | imported from each build's `builds/` record: gate name, status, failures |
| `catalog` | table or contract version | imported from `contracts/` and the dbt manifest: tables, columns, owners, contract versions |
| `drafts`, `snapshots` | draft / snapshot | a draft's changes, its snapshot id, build id, baseline release, status; a snapshot's id, contents hash and lake path |

`flags`, `dq_results` and `catalog` are imported copies of what builds and git already hold. They make the
database the place to look, but they are never a source a build reads.

### 2.3 Rule settings: code defines, the database configures

```yaml
schema: 1                       # generated from the code's rule types
rules:
  - {id: verdict_flip, family: classifier, polarity: {added: supports, removed: restore}}
  - {id: shared_body, family: catalog, polarity: doubts, params: {min_products: 3}}
  - {id: bulk_green_weight, family: listing, polarity: supports, proposes: exclude, params: {min_grams: 20000}}
readiness: {removal: {supports_min: 1, live_check: required, blocked_by: [doubts, coverage]}}
guard: {mode: remove_unless_doubted, doubted_by: [shared_body], max_held: 200, max_weeks: 4}
precedence: [roaster, product, variant]
action_groups: {visibility: [exclude, include, exclude_variant], batch: [accept, reject, use_batch]}
```

The coffee definition (the verdict prompt), the rule types' code, the registry seed and the contracts stay in git,
changed by PR. The admin shows them read-only, through `catalog`.

### 2.4 The review's four MAJORs, as settings, code and tests

| Finding | Setting | Enforced by code | dbt test |
|---|---|---|---|
| M1: doubt signals counted as votes to remove | `polarity` per rule (required: `supports` / `doubts` / `restore`); `readiness.removal` | readiness counts only `supports`; an open `doubts` or coverage flag blocks; for now, one flag plus a live check | `ready_removals_have_no_doubts` (error) |
| M2: the classifier's removals bypass review | `guard.mode`, `max_held`, `max_weeks` | a doubted removal keeps the product's last version that was coffee | `classifier_guard` (error over `max_held`) |
| M3: no single effective decision per subject | `precedence`, `action_groups` | the latest `approved` / `reverted` event per (subject, group) wins; roaster > product > variant | `unique` on `int_effective_decisions`; three conflict unit tests; `decision_orphaned` (warn: a platform move changes `coffee_id`) |
| M4: an audit trail behind a token that can rewrite the lake | none (deployment) | the admin writes the database and the private curation dataset (snapshots), and only reads the lake | a `terraform test` on the admin's grants |

The guard runs in every build, the weekly one included. Its scope is open question 1.

## 3. The workflow

```
 admin edit ──▶ version rows ──▶ snapshot exported to the lake (immutable, pinned)
                                     │
                                     ▼
                     draft build (all gates, recorded, not served)
                                     │
           Draft page: gates · build-diff · #111 rows in/out per table with reasons, vs the served release
                                     │
       Publish (the owner) ──▶ mark the snapshot published ──▶ serve the draft ──▶ export + site
       Discard ──▶ mark the draft discarded (its build stays recorded, as failed builds do)
```

### Drafts

Every admin change creates or extends a draft: decisions, a settings edit, or a crawl-flag change. The draft
build is the existing Cloud Run `build` job, started with `--draft` and the snapshot id. The admin service itself
never writes the lake.

### Publish

Publish is disabled until four things hold:
1. the draft build passed;
2. the served release is still the draft's baseline;
3. any required evaluation is attached;
4. the owner has typed the count, when more than 50 public rows leave.

Publish then marks the snapshot `published` and serves the draft with `serve(…, action="publish")`, followed by
the normal export and site, as `cmd_rollback` does. A retry re-serves the same build.

### Stale drafts

When a weekly release changes the baseline, the draft rebuilds itself on the new release. It runs its gates and
the smoke suite (`make smoke`), and it **never publishes**. It rebuilds at most once per new release. The owner sees the new diff and decides again.

### The weekly release

It stays automatic, gated by the existing tests. It reads the last **published** snapshot and never an open draft.

### The crawl flag

- **Off:** `select_states` skips the roaster, through the published snapshot.
- **On:** the roaster can be crawled again. For a new or under-covered roaster, turning it on also starts a
  one-roaster smoke crawl, `crawl --only <roaster>` (the crawl job with overrides, at most once a day per roaster),
  and then a draft build, so the owner sees its catalog before publishing.

The flag is a roaster-level decision, ranked above the `roasters.csv` seed; the admin never edits the CSV.

### Removals and restores

- A removal needs a `supports` flag, a live check of the store, no open doubt, and the owner's Publish.
- A roaster with a coverage problem is fixed, not removed.
- A restore (`include`) is a decision, published through the same flow.

### CLAUDE.md for settings changes

A settings edit that changes how a rule decides (a polarity, the guard's mode, readiness, a new rule), or that
changes more than 50 products on top of the field's running total, needs its evaluation attached before Publish.
That means an `eval/<rule>/` folder with its README, RESULTS and SHA256SUMS, at a commit on `main`. Smaller edits
take step 9's short path; the draft's diff is the list to check by hand.

## 4. dbt

- **Inputs.** The pinned snapshot is a `source()`. The settings reach dbt as vars plus a generated seed; no
  threshold is hard-coded.
- **New models**, each documented grain-first in `schema.yml`:
  - `stg_decisions`: one row per event;
  - `int_effective_decisions`: one row per subject × action group;
  - `int_catalog_decided`: one row per candidate product, covering the classifier, the effective decision, the
    guard and the outcome;
  - `mart_flags`.
- **Existing models.**
  - `dim_coffee` reads `int_catalog_decided` and keeps its meaning, so the app, MCP and site are unchanged.
  - The public tables filter, and their columns don't change.
  - `products.decide` takes the effective decision, so enrichment and the registry agree with the build.
- **Tests.**
  - `decisions_applied` (error). Never downgrade it to pass a draft.
  - `classifier_guard`, `ready_removals_have_no_doubts` and the `unique` test.
  - Dict-format unit tests for the conflicts and the guard.
  - `decision_orphaned` (warn).
- **Builds record** the snapshot id, the git SHA, the image digest, the `draft_id` and every decision id applied.

## 5. The admin

### 5.1 Pages (Phase 1)

| Page | Shows | Actions |
|---|---|---|
| **Queue** | open flags by subject, then roaster and rule, sorted by public rows affected; one summary per row ("1 supports · 1 doubts · held") | open a case; add to the draft |
| **Case** | each flag with its polarity and evidence (rules' answer, verdict, title, type, tags, sizes, prices, the description and how many share it, 8 weeks of fetches); the roaster's crawl flag | live check; propose exclude or include with a reason; toggle the crawl flag |
| **Draft** | its changes; build status; each gate's result; `build-diff`; #111's rows in and out per table with reasons; the count of public rows that leave | **Publish**, **Discard** |

Every page defines these states:
- **empty:** "Nothing to review", as of build X;
- **loading;**
- **building**, with progress;
- **stale:** rebuilding on the new release;
- **failed:** keeps the typed reason, retries with the same idempotency key, and says what was recorded.

A disabled Publish says why it is disabled.

**Design.** The pages reuse Bench Scale (`app/static/app.css`, `docs/app/DESIGN.md`) in both themes, with the
existing components. No new colours: `--danger` only on Publish's confirm when rows leave. No dashboard tiles. Every
state has a text label, and keyboard shortcuts can be turned off (WCAG 2.1.4). Before review, CLAUDE.md asks for
Playwright runs at 390 px and desktop, light and dark, plus an axe check.

### 5.2 API and safety

- **Endpoints.** The API is the only writer; `coffeedata decide` is a thin client.
  - Reads: `GET /api/queue`, `GET /api/cases/{subject}`, `GET /api/catalog`.
  - Drafts: `POST /api/drafts`, `POST /api/drafts/{id}/changes`, `POST /api/drafts/{id}/publish`,
    `POST /api/drafts/{id}/discard`.
  - Roaster state: `POST /api/roasters/{id}/crawl`.
- **Idempotency.** Writes carry an idempotency key (`decision_id` + `seq`) and the version they were based on, so a
  retry never doubles an event and a stale tab fails loudly.
- **Untrusted text.** Titles, tags and descriptions are rendered escaped, never `|safe`. The pages keep `app/web.py`'s
  CSP and `Referrer-Policy: same-origin`, link only to `https:` store URLs, and keep the app's CSRF rule.

### 5.3 Sign-in and access

The GCP project sits on a personal account with no organization, so IAP is out. Sign-in is **passkey-only**, built
into the admin, not tied to a Google account. Without IAP the service is reachable from the internet like the app
(an `allUsers` invoker), so every route but `/health` authenticates in code and fails closed:
- two registered passkeys and printed recovery codes;
- no password and no setup-code reset;
- 12-hour sessions that can be revoked server-side;
- CLI device tokens (12 hours, scoped, revocable) stored in the OS keychain;
- no magic links, and no long-lived personal API keys.

The rest follows the security review of 2026-11-04. It is held privately and not committed, because it lists open
weaknesses in the live app; this repo is public. In short:
- **Credentials.** The admin has its own least-privilege service account and credentials, separate from the app's:
  database access, write on the private curation dataset (snapshots), read on the lake, and the right to start the
  `build` and `crawl` jobs with overrides. It never uses the app's `HF_TOKEN`, and it holds no Anthropic key.
- **Deploys.** A ruleset protects `main`. An `admin` GitHub environment needs the owner's approval, and the deploy
  credential is bound to it. A `terraform test` checks the admin's grants (no lake write, its secrets only). The owner applies a reviewed plan, and
  `deploy.yml` deploys the app and the admin from one image.
- **Alerts and limits.** An email goes out on every admin write and on every settings, permission or service
  change. Write limits apply per identity.
- **The code fails closed.** No env var turns auth off, and the local stub refuses to run when hosted.

**Cost:** about $0 a month plus the database (section 7), and two hardware keys (about $50 each, once).

## 6. This week's cases

| Case | Flags | The draft shows |
|---|---|---|
| 16 Stivers single-origins removed (the same pasted "Sample Pack" text) | `verdict_flip` restore + `shared_body` doubts; the guard keeps them | 16 `include`s: 0 rows out, `held:guard` → `decision:include` |
| 3 drink mixes added | `verdict_flip` supports; a live check each | 3 rows out of `public_coffee`, `decision:exclude` |
| 5 businesses that sell no coffee | product `exclude`s after a live check; crawl flag off | their rows out, by roaster |
| 6 roasters we fail to crawl | coverage doubts block removal; fixed in code or crawl config | after the fix: crawl flag on → smoke crawl → draft |
| 22 green-coffee sacks | `bulk_green_weight`; a live check (one per group if question 2 says yes) | 22 rows out |
| Unsure verdicts (4,177 version rows) | none; deferred until verdict confidence is measured | — |

## 7. POC plan (Friday)

**Narrowed to 2 × 2:** Cloud SQL for PostgreSQL at its smallest size, with IAM database auth and point-in-time
restore, against one free tier (Neon), each with Go and with Python. Each of the four builds the same thin slice:
1. write a decision through the API;
2. record its version row;
3. export a snapshot to a scratch curation dataset;
4. start a draft build (a scratch build job or a local `coffeedata build --draft`) that reads the pinned snapshot.

| Criterion | Pass if |
|---|---|
| The slice works end to end | the draft build applies the decision, and a second run with the same snapshot id gives the same rows |
| Cost | the measured monthly figure at our volume is written down (Cloud SQL's estimate is about $9–10) |
| Passwordless auth | Cloud Run reaches the database with no password in Secret Manager or Terraform state (IAM auth, or the free tier's equivalent); otherwise record exactly what credential is needed |
| Backups | a point-in-time or branch restore is done once and timed |
| Cold start | p50 and p95 of the first admin request after idle, with the database idle too (Neon suspends) |
| Lock-in | `pg_dump` from one restores into the other with no code change |
| Language | lines of code; image size; cold start; how it reads flags and the diff (Parquet), and starts the build |

**Notes for judging.**
- Draft builds run as the existing Python job whatever the API's language, so Go doesn't need to call the build
  in-process.
- Python reuses `coffeedata` for the diff, the flags and serving.

The owner decides from the results. Nothing from a POC merges without the review cycle.

## 8. Phasing

**Phase 1** (after the POC decision): change something in the admin, see the draft's diff, then Publish or Discard.
- The database with its version tables, and the snapshot export.
- `coffeedata build --draft --snapshot` and `serve(action="publish")`.
- dbt applying `exclude`, `include` and `exclude_variant`, with the M1–M3 tests.
- The guard, in the mode question 1 settles, after its CLAUDE.md evaluation.
- The three rules.
- The API, `coffeedata decide`, and passkey sign-in.
- The Queue, Case and Draft pages.

Phase 1 depends on #111 for per-row reasons. Until #111 merges, the Draft page shows `build-diff`.

**Later.**
- The crawl flag with its smoke crawl.
- Stale-draft auto-rebuild.
- Settings editing in the admin.
- The catalog pages (`dq_results`, contracts).
- Group approvals and history.
- `not_a_coffee_seller`.
- More rule types, each scored.
- Runbook entries: a bad publish (roll back, then `revert`), a failed write, a lost passkey (the recovery codes).
- #110's blocked-store rule.
- Green coffee by the sack, after verdict confidence is measured.

## 9. Open questions for the owner

1. **Guard scope.** When a verdict flips a public coffee to not coffee, should it be removed unless something
   looks suspicious, such as the same pasted text across a store's catalog (recommended: `remove_unless_doubted`)?
   Or should every such removal be held for review? Holding them all would have queued about 3,765 in the backfill.
2. **Group approval.** Can one live check of a store approve a whole group with the same evidence, for example all
   16 Stivers coffees? Recommended: yes, for members whose `evidence_hash` matches the checked sample.

## Skills used

- **dbt and data quality** (2, 4): dbt Labs `using-dbt-for-analytics-engineering`, `adding-dbt-unit-test`,
  `troubleshooting-dbt-job-errors`; wshobson `data-quality-frameworks`.
- **DevOps** (3, 5.3, 7): `terraform-skill`, HashiCorp `terraform-test`; wshobson `python-resilience`,
  `deployment-pipeline-design`.
- **Design** (5.1): MengTo `no-ai-design-slop`; Owl-Listener `state-machine`, `accessibility-audit`.
