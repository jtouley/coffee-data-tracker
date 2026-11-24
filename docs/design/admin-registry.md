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
3. **Builds never read the live database.** The admin API exports an immutable snapshot of the settings and
   decisions to the private curation dataset. Builds read only that snapshot, pinned by id, and hold no database
   credential.
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

**Drafts are marked as drafts.** Every `builds/` row of a draft carries its `draft_id`. Until a draft is published,
these leave it out:
- `rollback`'s default ("the passing release before the served one");
- `build-diff`'s default `new`;
- the anomaly bands and the volume baseline.

A draft writes no personal marts, and only Publish can serve it. `rollback(to=<id>)` refuses an unpublished draft's
id.

## 2. The database

### 2.1 Why a database, and not git (this overrules the platform review)

The platform review (issuecomment-6641959444) recommended rule settings in git and decisions in an HF dataset.
The owner chose a database, because **settings edited in the admin panel need one behind them**: git can't be their
home if the panel is where they change. Here is how each of the review's concerns is kept:

| The review's concern | How it is kept |
|---|---|
| Exact rebuilds (one SHA pins code and params) | Builds read only an immutable **snapshot** in the curation dataset, never the live database. The build records the snapshot id, the git SHA and the image digest |
| Audit and review | Every change is a new version row: who, when, why, the old value and the new one. The draft build is the review: its gates and diff |
| Config and image skew | The settings schema is generated from the code's rule types. The API rejects a value the deployed schema doesn't allow, and a draft build fails on a snapshot its image can't read |
| A database down breaks the weekly build | It can't. The weekly build finds the published snapshot from `serving_log` (3.2), with no database. A database outage only blocks admin edits |
| A password in Secret Manager and state | Passwordless database auth is a POC success criterion (section 7) |
| Back up off one store | The database has point-in-time restore. Every snapshot is in the private curation dataset, and each build's release folder keeps its effective decisions |
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
| `drafts`, `draft_events`, `snapshots` | draft / status event / snapshot | a draft's changes, snapshot id, build id and baseline release, with its status as events (built, discarded, published); a snapshot's id, contents hash and path |

`flags`, `dq_results` and `catalog` are imported copies of what builds and git already hold. They make the
database the place to look, but they are never a source a build reads.

### 2.3 Rule settings: code defines, the database configures

```yaml
schema: 1                       # generated from the code's rule types
rules:
  - {id: verdict_flip, family: classifier, polarity: {added: supports, removed: restore}}
  - {id: shared_body, family: catalog, polarity: doubts, params: {min_products: 3}}
  - {id: non_coffee_type, family: listing, polarity: supports, proposes: exclude, params: {types: [drink mix, nuts, books, parts]}}
readiness: {removal: {supports_min: 2, distinct_families: true, live_check: required, blocked_by: [doubts, coverage]}}
guard: {mode: remove_unless_doubted, doubted_by: [shared_body], max_held: 200, max_weeks: 4}
precedence: [roaster, product, variant]
action_groups: {visibility: [exclude, include, exclude_variant], batch: [accept, reject, use_batch]}
```

The coffee definition (the verdict prompt), the rule types' code, the registry seed and the contracts stay in git,
changed by PR. The admin shows them read-only, through `catalog`.

### 2.4 The review's four MAJORs, as settings, code and tests

| Finding | Setting | Enforced by code | dbt test |
|---|---|---|---|
| M1: doubt signals counted as votes to remove | `polarity` per rule (required: `supports` / `doubts` / `restore`); `readiness.removal` | readiness needs `supports` flags from **two distinct families** (the owner's "several signals"), plus a live check; an open `doubts` or coverage flag blocks | `ready_removals_have_no_doubts` (error) |
| M2: the classifier's removals bypass review | `guard.mode`, `max_held`, `max_weeks` | a doubted removal keeps the product's last version that was coffee | `classifier_guard` (error over `max_held`) |
| M3: no single effective decision per subject | `precedence`, `action_groups` | the latest `approved` / `reverted` event per (subject, group) wins; roaster > product > variant | `unique` on `int_effective_decisions`; three conflict unit tests; `decision_orphaned` (warn: a platform move changes `coffee_id`) |
| M4: an audit trail behind a token that can rewrite the lake | none (deployment) | the admin writes the database and the private curation dataset (snapshots), and only reads the lake | a `terraform test` on the admin's grants |

The guard runs in every build, the weekly one included.
- **Mode `remove_unless_doubted`** (the owner chose it): a verdict flip is removed, unless a doubt such as
  `shared_body` applies.
- **Overrides.** Every removed coffee can be reviewed in the admin and flipped back. An `include` keyed on
  `coffee_id`, or on the product's input hash, is a versioned decision row that feeds the next draft like any
  other change.

## 3. The workflow

```
 admin edit ──▶ version rows ──▶ admin API exports a snapshot to curation:snapshots/<id>/ (immutable)
                                     │
                                     ▼
              build job: build --draft <draft> --snapshot <id> (all gates, recorded as a draft, not served)
                                     │
           Draft page: gates · build-diff · #111 rows in/out per table with reasons, vs the served release
                                     │
       Publish (the owner) ──▶ admin starts the publish job ──▶ serve(action="publish") ──▶ export + site
       Discard ──▶ a `discarded` draft event (the build stays recorded, as failed builds do)
```

### Drafts and snapshots

- **Changes.** Every admin change creates or extends a draft: decisions, a settings edit, or a crawl-flag change.
- **The snapshot.** The **admin API** exports it to `snapshots/<id>/` in the private curation dataset: settings,
  effective decisions and roaster state as Parquet, plus a contents hash. The build never exports it, and holds no
  database credential.
- **The build.** The draft build is the existing Cloud Run `build` job, started with `--draft` and the snapshot id.

### Publish: who runs it, and in what order

The admin service can't write the lake. Publish is a **`publish` Cloud Run job**, from the ingest image, running as
an identity with lake write. The admin can only start it, like the other named pipeline jobs (the Jobs section).

Publish is disabled until four things hold:
1. the draft build passed;
2. the served release is still the draft's baseline;
3. any required evaluation is attached;
4. the owner has typed the count, when more than 50 public rows leave.

The job then runs these steps, in this order:
1. **serve first:** `serve(…, action="publish")` writes `marts/`, the release pointer and a `serving_log` row;
2. **then the export and the site**, as `cmd_rollback` does.

**"Published" isn't a status anyone sets.** It is derived: the snapshot of the build in the latest `serving_log`
row. So a failed serve leaves the snapshot unpublished. A failed export after the serve is retried by re-serving
the same build, which is idempotent. A `rollback` also rolls back to that build's snapshot, which is the meaning a
rollback should have.

### Stale drafts

When a weekly release changes the baseline, the draft rebuilds itself on the new release, in the build job. It
runs every gate, and a diff against the draft's previous build shows what changed. It rebuilds at most once per
new release and **never publishes**; the owner sees the new diff and decides again.

`make smoke` is not part of this: it tests code over fixtures, and runs in CI on code changes. The build image has no
tests.

### The weekly release

It stays automatic, gated by the existing tests. It reads the published snapshot (the served build's, from
`serving_log`) and never an open draft.

### The crawl flag

- **Off:** `select_states` skips the roaster, through the snapshot the crawl was given. That includes the `only`
  branch, which today returns a roaster without the usual filters.
- **Which snapshot.** Scheduled crawls read the published snapshot. The admin-started smoke crawl is given the
  **draft's** snapshot id as an argument when the admin starts the `crawl` job, so a draft that turns the flag on
  is read as on.
- **On:** the roaster can be crawled again. For a new or under-covered roaster, turning it on also starts a
  one-roaster smoke crawl, `crawl --connector <its connector> --only <roaster>` (the crawl job with overrides, at most once a day),
  and then a draft build, so the owner sees its catalog before publishing.

The flag is a roaster-level decision, ranked above the `roasters.csv` seed; the admin never edits the CSV.

### Test crawl (on demand)

The owner: "Shouldn't that run, by connector be something I can run in the admin panel?" This is the one-store check
in RUNBOOK §2.1, run after an egress change: does the new NAT IP get past SiteGround's challenge?

- **Where.** On a roaster's page, and per connector, for a sample of that connector's stores (for example 5
  roasters that answered `sg-captcha`).
- **What it runs.** The admin starts the `crawl` job with `--connector` and `--only` for each roaster. This is the
  same path as the crawl-flag smoke crawl, which is a test crawl plus a draft build.
- **What it shows,** per roaster:
  - the fetch's status and HTTP code;
  - `challenge` (`siteground` or `cloudflare`);
  - the job manifest's `egress_ips` and `detail.egress`;
  - the Cloud Run execution name.

  It keeps a summary in a `test_crawls` table.
- **It never counts as the week's crawl.**
  - On `main`, `crawl --only` writes into the week's own partition (`raw/connector=X/run_date=W/`), and the weekly
    merge reads it. So the RUNBOOK's manual check today becomes that roaster's fetch for the week.
  - The test crawl instead reuses the confirmation arms' path. It gets a sub-partition of its own, `test=<id>` (as
    `slice=` does), which the merge never reads. `detail.arm = "test"` makes `control.is_arm` keep it out of
    `landed` and the egress flags.
  - A smoke crawl's draft build adds that sub-partition as an extra lake root (`dbt_build`'s `extra_roots`, as
    fetch-health does). The weekly build never adds it.
- **Limits.** Once a day per roaster, as for the smoke crawl. It is refused from the Monday crawl window's start
  (09:00 UTC) until the week's merge has a `done` row. It follows the same robots.txt and pacing as any crawl.
- **Phase 1**, since it shares the smoke-crawl plumbing.

Until the admin ships, RUNBOOK §2.1's check stays a `gcloud run jobs execute … --only` command. After that, it
becomes this button.

### Jobs (on demand, replacing gcloud)

The owner: "shouldn't I also be able to run full crawls off cycle as well from the admin panel? … the admin panel
should be able to move the CLI, no?" The Jobs page and API replace the owner's `gcloud run jobs execute` use for the
pipeline.

**What it can start.** Every pipeline job named in `ingestion.tf`, with the args the CLI takes:
- a crawl for one connector, or all of them;
- `crawl-retry`, `merge`, `enrich-week` (classify and enrich) and `fetch-health`;
- `build`, normal or `--draft`;
- `publish` (new, section 3), which also re-exports and rebuilds the site files. The GitHub Pages deploy stays in
  `site.yml`, since the admin holds no GitHub token.

**What it shows.** For each execution: live status, a link to its logs in the Cloud console, the exit code and the
duration. It keeps a history in a `job_runs` table, and it can cancel a running execution.

**Off-cycle full crawls: two explicit modes.** On `main`, a crawl writes into `run_date=<week>`, and the merge reads
that week.
- **(b) "test", the default:** it writes into a `test=<id>` sub-partition, as Test crawl does. The weekly build
  never reads it.
- **(a) "count for week W", only if the owner picks it:** it writes into W's partition, like the scheduled passes.
  - It is allowed only before W's merge has a `done` row in `job_manifests`.
  - After that it is refused, unless the owner also starts the merge again and a draft build.
  - A released week is never rewritten. A draft over it is published only through Publish.

**Guards.**
- **No overlap.** It refuses a job that already has a running execution; that is `cutover2.sh`'s step 1 check.
- **Scheduled windows.** It refuses inside the Monday crawl and Tuesday release windows, unless the owner overrides
  explicitly, and that override is recorded.
- **Confirm.** A confirm step shows the exact args before the start.
- **Crawling rules unchanged.** Per-store pacing and robots.txt rules don't change.
- **Audit.** Every start, cancel and override is an audit row: who, the job, the args, the mode, and the execution
  name. Each also triggers the write email.

**Access.**
- The admin's service account gets `run.jobs.runWithOverrides` (args included), plus
  `run.executions.get/list/cancel`, on the named jobs only.
- It gets no Terraform, IAM or project-level roles. The `publish` job is still the only lake writer for serving.
- **Infra stays on the CLI:** `terraform apply`, image builds and digest pins (`cutover2.sh`), and Scheduler
  changes. These change what the jobs *are* and what can reach them, not what they process. Giving the admin those
  roles would let a stolen admin session rewrite its own guards, which the security review rules out.

**CLI.** `coffeedata admin jobs run|status|cancel` calls the same API, so "the API is the only writer" holds for jobs
too.

**Phase 1.** It reuses the Test crawl's plumbing (start a job with overrides, poll the execution, write an audit
row), so it is mostly a page over the same client.

### Removals and restores

- A removal needs `supports` flags from two distinct families, a live check of the store, no open doubt, and the
  owner's Publish.
- **Group approval** (the owner: yes): one live check covers the members of a group whose `evidence_hash` matches the
  checked sample.
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

### Corrections feed labeling, and a keyword pre-filter

The owner, on 2026-11-06: corrections are "fixable in admin panel", which "would [allow] us to better train the
labeling", and "further keyword filtering before the labeling would help remove more objects before LLM
classification".

**Corrections as labels.** Each `include` or `exclude` override is a versioned decision row, so it is also a labeled
example.
- `coffeedata decide export-labels` writes the overrides as candidate reference labels for the classifier eval:
  `input_hash`, the label, the reason and the actor.
- Under CLAUDE.md, an owner correction is **one labeler**. It goes into the train set only.
- It reaches a sealed test set only if a second labeler labels it independently against `GUIDE.md`, and a third
  settles any disagreement.
- **The bias:** corrections exist only where something was flagged or looked wrong, so they are not a random sample.
  They show where the classifier breaks, not how often.

**A keyword pre-filter before the LLM.** Part of this exists already. `verdict.pending` skips every product
`classify.excluded` rules out (gear, merch, tea, green coffee and the like), so those never reach Claude.
- **What it adds.** Admin-configured keyword and product-type rules, a `prefilter` rule type in the same settings
  schema, that extend `excluded`. They are not a second list.
- **What they do.** They drop obvious non-coffee before Claude classifies it, to save cost. When unsure, they
  abstain and leave the product for Claude.
- **How it ships.** It is measured like any heuristic under CLAUDE.md: a held-out split grouped by roaster, Wilson
  intervals, and no meaningful rise in confidently wrong answers. Each settings version takes the draft flow and
  the evaluation gate (section 3).
- **When.** It comes in Later, once the Phase 1 decision log exists to measure against.

**Ties prefer "coffee".** When the verdicts tie, the product is kept as coffee. That change is being made in a
separate PR.

## 5. The admin

### 5.1 Pages (Phase 1)

| Page | Shows | Actions |
|---|---|---|
| **Queue** | open flags by subject, then roaster and rule, sorted by public rows affected; one summary per row ("1 supports · 1 doubts · held") | open a case; add to the draft |
| **Case** | each flag with its polarity and evidence (rules' answer, verdict, title, type, tags, sizes, prices, the description and how many share it, 8 weeks of fetches); the roaster's crawl flag | live check; propose exclude or include with a reason; toggle the crawl flag; **Test crawl** |
| **Catalog** | **every** coffee in the published release, flagged or not: keyword search, and filters on roaster, origin, process, coffee or not, listed or removed (and why), stock and price | open any coffee; propose exclude or include (the override) into the draft |
| **Jobs** | every pipeline job, its running and past executions (status, exit code, duration, logs link), with the mode and args | run (with a confirm showing the args), cancel |
| **Draft** | its changes; build status; each gate's result; `build-diff`; #111's rows in and out per table with reasons; the count of public rows that leave | **Publish**, **Discard** |

Every page defines these states:
- **empty:** "Nothing to review", as of build X;
- **loading;**
- **building**, with progress;
- **stale:** rebuilding on the new release;
- **failed:** keeps the typed reason, retries with the same idempotency key, and says what was recorded.

A disabled Publish says why it is disabled.

**The Catalog's read path.** It reads the published release (`marts/` and the public tables of the served build)
and the published snapshot. The admin loads them once per release into a local DuckDB file, and searches and filters
there. It never scans the live lake on each request.

**Design.** The pages reuse Bench Scale (`app/static/app.css`, `docs/app/DESIGN.md`) in both themes, with the
existing components. No new colours: `--danger` only on Publish's confirm when rows leave. No dashboard tiles. Every
state has a text label, and keyboard shortcuts can be turned off (WCAG 2.1.4). Before review, CLAUDE.md asks for
Playwright runs at 390 px and desktop, light and dark, plus an axe check.

### 5.2 API and safety

- **Endpoints.** The API is the only writer. `coffeedata decide` is a thin client, as is the admin MCP server
  (5.2.1).
  - Reads: `GET /api/queue`, `GET /api/cases/{subject}`, `GET /api/catalog`.
  - Drafts: `POST /api/drafts`, `POST /api/drafts/{id}/changes`, `POST /api/drafts/{id}/publish`,
    `POST /api/drafts/{id}/discard`.
  - Roaster state: `POST /api/roasters/{id}/crawl`.
  - Test crawls: `POST /api/test-crawls` (a connector and one or more roaster ids), and `GET /api/test-crawls/{id}`.
  - Jobs: `POST /api/jobs/{job}/runs` (args, mode, any override), `GET /api/jobs/runs`, and
    `POST /api/jobs/runs/{execution}/cancel`.
- **Idempotency.** Writes carry an idempotency key (`decision_id` + `seq`) and the version they were based on, so a
  retry never doubles an event and a stale tab fails loudly.
- **Untrusted text.** Titles, tags and descriptions are rendered escaped, never `|safe`. The pages keep `app/web.py`'s
  CSP and `Referrer-Policy: same-origin`, link only to `https:` store URLs, and keep the app's CSRF rule.

### 5.2.1 The admin MCP server: a fourth client

The owner: "While an MCP for admin is fine, wrapping the mcp without a login just leaves me stuck on gcloud".
So the admin MCP server is one more client of the admin API, beside the UI, the CLI and the Jobs page. It is
separate from the existing `coffeedata` MCP server (`mcp_server.py`: shots and the public catalog).

- **Auth.** It signs in as the owner with the same short-lived CLI device token: kept in the OS keychain, 12 hours,
  revocable. It has no credentials of its own: no gcloud ADC, no HF token, no service-account key.
- **Tools.** Each tool is a thin call to an existing endpoint:
  - catalog search;
  - the week's status and its failures;
  - release changes;
  - decisions;
  - draft and publish;
  - jobs;
  - test crawls.
- **Writes.** They go through the same confirm-and-audit path, recorded with `client=mcp`.
- **Read-mostly.** Publish and job starts need an explicit `confirm` argument, and they are refused in the Monday and
  Tuesday windows like everywhere else.
- **Why not wrap gcloud or the lake directly:** that MCP would carry its own credentials and bypass sign-in, the
  audit rows and every guard (the windows, the overlap check, the readiness and Publish gates).
- **When: Later, first.** It is thin, but every tool depends on Phase 1's endpoints and device tokens. Building it
  after they settle avoids wrapping a moving API.

### 5.3 Sign-in and access

The GCP project sits on a personal account with no organization, so IAP is out. Sign-in is **passkey-only**, built
into the admin, not tied to a Google account. Without IAP the service is reachable from the internet like the app
(an `allUsers` invoker), so every route but `/health` authenticates in code and fails closed:
- two registered passkeys and printed recovery codes;
- no password, and no **standing** setup code: the app's `OWNER_SETUP_CODE` can reset the owner at any time, and
  nothing like it exists here;
- 12-hour sessions that can be revoked server-side;
- CLI device tokens (12 hours, scoped, revocable) stored in the OS keychain;
- no magic links, and no long-lived personal API keys.

**Enrolling the first passkey.**
- Terraform creates an empty Secret Manager secret, `ADMIN_ENROLL_TOKEN`. The owner adds one random value from the
  laptop, so no value sits in Terraform state.
- `/enroll` accepts that token only while **no passkey exists**, and only once. Registering the first passkey records
  the token's hash as spent, and closes `/enroll` for good.
- The second passkey is added from that signed-in session.
- Later, adding or replacing a passkey needs a signed-in passkey session plus one recovery code. Losing both passkeys
  means a recovery code, which works once.

This is a one-shot bootstrap, not a reset path, so it doesn't contradict "no setup code".

**Reachable signed out:** `/health`, the sign-in and WebAuthn routes, `/enroll` (closed after the first passkey) and
static assets. A test walks every registered route signed out and expects 401 or 403 on the rest.

The rest follows the security review of 2026-11-04. It is held privately and not committed, because it lists open
weaknesses in the live app; this repo is public. In short:
- **Credentials.** The admin has its own least-privilege service account and credentials, separate from the app's:
  database access, write on the private curation dataset (snapshots), read on the lake, and the right to start the
  named pipeline jobs with overrides, and to list and cancel their executions (the Jobs section). Only the jobs hold
  lake write. It never uses the app's `HF_TOKEN`, and it holds no Anthropic key.
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
| 3 drink mixes added | `verdict_flip` (classifier) + `non_coffee_type` (listing) support; a live check each | 3 rows out of `public_coffee`, `decision:exclude` |
| 5 businesses that sell no coffee | the same two families on their products, plus a live check; crawl flag off | their rows out, by roaster |
| 6 roasters we fail to crawl | coverage doubts block removal; fixed in code or crawl config | after the fix: crawl flag on → smoke crawl → draft |
| 22 green-coffee sacks | deferred by the owner until verdict confidence is measured (Later) | — |
| Unsure verdicts (4,177 version rows) | none; deferred until verdict confidence is measured | — |

## 7. POC plan (Friday)

**Narrowed to 2 × 2** by the TPM review, from the owner's seven candidates: Cloud SQL for PostgreSQL at its
smallest size (IAM database auth, point-in-time restore) against one free tier (Neon), each with Go and with
Python. Each of the four builds the same thin slice, against a **scratch lake and a scratch curation dataset**
(`COFFEEDATA_LAKE` names the scratch one, never production):
1. write a decision through the API;
2. record its version row;
3. export a snapshot;
4. start a draft build that reads the pinned snapshot.

| Criterion | Pass if |
|---|---|
| The slice works end to end | the draft applies the decision, and two builds of the same snapshot with the same `today` give equal `table_hash`es: a hash over each public table's rows, without `release_build_id` and `release_run_date` |
| Cost (an estimate) | at ≤ 1,000 decision writes, ≤ 20 snapshots and ≤ 20 draft builds a week, and ≤ 1 GB stored, the monthly estimate (egress to Cloud Run included) is **≤ $15** |
| Passwordless auth | **pass:** Cloud Run connects with no static database password in Secret Manager, Terraform state or env vars. **Fail:** anything else |
| Backups | a point-in-time or branch restore is done once, and takes ≤ 15 minutes |
| Cold start | 10 samples of the first admin request after 30 minutes idle (the service and the database): **p95 ≤ 3,000 ms**; warm p95 ≤ 300 ms |
| Lock-in | `pg_dump` from one restores into the other with no code change |
| Language (a measurement for the owner, not pass/fail) | lines of code; image size; cold start; how it reads Parquet and starts the build job |

**Notes for judging.**
- Draft builds run as the existing Python job whatever the API's language, so Go doesn't need to call the build
  in-process.
- Python reuses `coffeedata` for the diff, the flags and serving.

The owner decides from the results. Nothing from a POC merges without the review cycle.

### 7.1 Decision: Python (2026-11-07)

The owner accepted **Python**: FastAPI, from the same image as the app. These are the local POC's numbers
(issuecomment-6699824051), 10 samples each, from `docker run` to the first signed-in 200:

| | Python | Go |
|---|---|---|
| Container cold start p95 | 938 ms | 273 ms |
| Lines of code | 218 | 473 |
| Image | the app's image, plus about 20 MB (psycopg) | a second image, 29 MB |

**Why Python.**
- **It reuses `coffeedata` in-process:** `served`, `build_diff`, `builds` and `serve`. Go would reimplement them or
  shell out to jobs.
- **About half the code.**
- **One image for the app and the admin**, which §5.3's deploy rule needs. Go would add a second image, and its
  Catalog DuckDB needs cgo.

**The revisit trigger.** If the admin's cold start p95 on real Cloud Run goes over about 2 s, the owner revisits Go
against Python. The local numbers don't include scale-from-zero, image fetch, or a database waking up.

**The alert that watches it** goes in the admin's Terraform, in the style of `src/terraform/alerts.tf`:
- **The metric** is `run.googleapis.com/container/startup_latencies`, which Cloud Run documents
  (https://cloud.google.com/monitoring/api/metrics_gcp_p_z, "Container startup latency").
  - It is BETA.
  - It is a DELTA distribution, in ms, on `cloud_run_revision`, sampled every 60 s.
- **What it measures:** the time to start a new container instance, not "the first 200". It is the closest
  documented metric for a cold start, but it leaves out the first request's own work, such as the database
  connection.
- **Which condition type:** a PromQL condition, because a plain threshold can't also require a minimum number of
  samples.

```hcl
resource "google_monitoring_alert_policy" "admin_cold_start" {
  display_name = "admin.registry.coffee: cold start p95 > 2 s"
  combiner     = "OR"
  conditions {
    display_name = "container startup p95 over 1 h, at least ${var.admin_cold_start_min_samples} starts"
    condition_prometheus_query_language {
      query    = <<-EOT
        histogram_quantile(0.95, sum by (le) (increase(run_googleapis_com:container_startup_latencies_bucket{monitored_resource="cloud_run_revision",service_name="${google_cloud_run_v2_service.admin.name}"}[1h]))) > 2000
        and on() sum(increase(run_googleapis_com:container_startup_latencies_count{monitored_resource="cloud_run_revision",service_name="${google_cloud_run_v2_service.admin.name}"}[1h])) >= ${var.admin_cold_start_min_samples}
      EOT
      duration = "0s"
    }
  }
  documentation {
    content   = "admin cold start p95 > 2 s: revisit the Go vs Python decision (docs/design/admin-registry.md §7.1)."
    mime_type = "text/markdown"
  }
  alert_strategy { auto_close = "86400s" }
  notification_channels = [google_monitoring_notification_channel.owner.id]
}
```

- **`admin_cold_start_min_samples`** (default 3) means a single slow start never pages.
- **The notification** goes to the existing owner channel.
- **Before the PR:**
  - check the PromQL name mapping for this BETA metric (`run_googleapis_com:container_startup_latencies_*`) in
    Metrics Explorer;
  - check that `condition_prometheus_query_language` exists in `hashicorp/google ~> 6.0`.

## 8. Phasing

**Phase 1** (after the POC decision): change something in the admin, see the draft's diff, then Publish or Discard.
- The database with its version tables, and the snapshot export.
- `coffeedata build --draft --snapshot` and `serve(action="publish")`.
- dbt applying `exclude`, `include` and `exclude_variant`, with the M1–M3 tests.
- Drafts marked in `builds/` and kept out of the default rollback, the default diff and the bands.
- The `publish` job.
- The guard in `remove_unless_doubted` mode, after its CLAUDE.md evaluation, with the per-coffee override.
- Three rules: `verdict_flip`, `shared_body`, `non_coffee_type`.
- Readiness from two distinct families, and group approval.
- The API, `coffeedata decide`, passkey sign-in with its one-shot enrollment.
- The Queue, Case, **Catalog** and Draft pages.
- Test crawl, per roaster and per connector, in its own `test=<id>` sub-partition.
- The Jobs page and `coffeedata admin jobs`, with the guards and the two crawl modes.
- The admin cold-start alert (§7.1), which ships with the admin service.

Phase 1 depends on #111 for per-row reasons. Until #111 merges, the Draft page shows `build-diff`.

**Later.**
- The admin MCP server (5.2.1), first: a thin client once Phase 1's API and device tokens settle.
- The crawl flag with its smoke crawl.
- Stale-draft auto-rebuild.
- Settings editing in the admin.
- Data-catalog pages for `dq_results` and contracts (apart from Phase 1's coffee Catalog page).
- History.
- `not_a_coffee_seller`.
- More rule types, each scored.
- Runbook entries: a bad publish (roll back, then `revert`), a failed write, a lost passkey (the recovery codes).
- #110's blocked-store rule.
- Green coffee by the sack (`bulk_green_weight`), after verdict confidence is measured.
- Overrides exported as candidate labels (train only), and the keyword pre-filter extending `classify.excluded`
  (section 4).

**Follow-ups from the review** (issuecomment-6692290541, minors):
- 8: status events instead of status columns, done for drafts here, and to check elsewhere;
- 9: label the settings example's values as defaults;
- 12: the crawl toggle and the stale state appear in Phase 1 pages, but their features are in Later;
- 13: the POC drops five candidates; this is now attributed to the TPM review;
- 15: how a CLI device token is issued (approved in a signed-in browser session);
- 16: the HF token's scopes need a startup check, since Terraform can't test them; and the first `*.tftest.hcl`;
- 17: "publishing" and "session expired" page states, and what Retry does after each failed step;
- 18: the readers of `serving_log`'s new `publish` action.

## 9. Open questions for the owner

None at the moment. The owner settled the last two (2026-11-06):
- **The guard removes unless doubted.** Every removal is reviewable in the Catalog, and can be overridden per coffee.
- **Group approval is yes**, for members whose evidence matches the checked sample.

The database and the language wait on the POC.

## Skills used

- **dbt and data quality** (2, 4): dbt Labs `using-dbt-for-analytics-engineering`, `adding-dbt-unit-test`,
  `troubleshooting-dbt-job-errors`; wshobson `data-quality-frameworks`.
- **DevOps** (3, 5.3, 7): `terraform-skill`, HashiCorp `terraform-test`; wshobson `python-resilience`,
  `deployment-pipeline-design`.
- **Design** (5.1): MengTo `no-ai-design-slop`; Owl-Listener `state-machine`, `accessibility-audit`.
