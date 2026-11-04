# admin.registry.coffee: a review queue for data decisions

Status: proposal (2026-11-04). Nothing here is built. Owner: jtouley.

**The ask.** The owner wants flags about registry.coffee's data raised automatically, backed by metadata, and
reviewed in one place before anything is removed: "Before we remove things, they need flagging [from] multiple
issues", "an admin ability to remove them, rather than how we have these datasets right now", and "what owns the
contracts of the coffees we're getting, how are they established, how do we edit them from the control plane?"

**The recommendation, in short.**
1. The core is a **data model, not a website**: an append-only `decisions` log on top of pipeline-raised `flags`,
   in the lake, applied softly by dbt. Every UI below reads and writes the same two tables.
2. Build the **admin app**, as a small server-rendered service from the existing app's image, on its **own Cloud
   Run service behind IAP**. It is the only option that shows private evidence (roasters' descriptions) to one
   person, records an audit trail, and costs about nothing.
3. **Start locally.** The first slice is the tables, the flags for this week's cases, dbt applying decisions, and
   `make admin` on the owner's laptop. Hosting at admin.registry.coffee comes second.
4. The owner's choice is right for *items*, and wrong for *definitions*. The coffee definition, the rules and the
   contracts stay in git, changed by PR and measured (CLAUDE.md). The admin shows them read-only and links to a PR.

## 1. Today (checked in the code on `origin/main`, 28a1b05)

| Piece | Where it lives | What it can do |
|---|---|---|
| Coffee definition | `src/coffeedata/coffee_definition.md` (the verdict prompt; `verdict.DEFINITION_VERSION` is its sha256, 12 hex) and `eval/sealed/GUIDE.md` (labeling; same text, different title line). Green coffee is `not_coffee` in both | Changed by PR only. A change makes every product version pending for Claude again (verdicts are kept per definition) |
| Rules | `classify.excluded` / `is_coffee`; `products.decide` (verdict wins unless the rules exclude outright), shared by `stg_products`, enrichment and the registry | Code |
| Verdicts | lake `verdicts/` (input_hash, model, definition, label). Local read-only copy after the 11-04 backfill: 41,315 coffee, 16,743 not_coffee, 4,177 unsure version rows. `unsure` falls back to the rules | Written by `classify` |
| Registry | `config/roasters.csv` (3,203 rows, git) + lake `registry/source=…` (discovery, the add-roaster issue form, opt-outs) | `stg_registry`, `registry.STATE_SQL` |
| Not a roaster | `registry.NOT_A_ROASTER_SITE`, 29 platform hosts (facebook.com, square.site, tryperdiem.com…) hard-coded; `registry.current` skips them | Code |
| Overrides | lake `run_overrides/`, one row per call: roaster-week, `accept` / `reject` / `use_batch`, a required reason. `stg_run_overrides` keeps the latest per roaster-week | `coffeedata override` (CLI) only |
| Release gates | `quality.py` + dbt tests; #111 (open) adds `release_changes` with a reason per changed public row | Automated |
| Contracts | `contracts/public-catalog.odcs.yaml` 2.3.0 (team: jtouley, owner), ODPS product, internal raw 1.5.0 / manifest 1.1.0 / enrichment 1.1.0 | `registry`, `verdicts` and `run_overrides` are in **no** contract |

Missing, as the owner said: no product- or variant-level override, no roaster status for "sells no coffee", no
review queue, and nothing that holds a removal until a second signal agrees. `dim_coffee` drops a product as soon as
its latest version isn't coffee ("drops out entirely"), so a wrong verdict removes it with no trace but the build diff.

## 2. The data model

Two append-only lake tables, beside the existing ones, never edited and never deleted (`Lake.write` refuses to
overwrite). Both live in the private lake; neither reaches a public table.

### 2.1 `flags`: what the pipeline noticed (machine-written)

`flags/run_date=<d>/build=<id>.parquet`, one file per build, written by `coffeedata build` from a dbt model
(`mart_flags`).

| Column | Meaning |
|---|---|
| `flag_id` | `sha256(subject_kind, subject_id, rule, proposed_action)[:16]`: stable across weeks, so "seen 3 weeks" is a count |
| `build_id`, `run_date`, `raised_at` | the build that raised it |
| `subject_kind`, `subject_id` | `product` (coffee_id), `variant` (coffee_id + variant_id), `roaster` (roaster_id), `batch` (roaster_id, run_date, batch_id), `store` (crawl target: host + adapter) |
| `rule` | the signal's name, e.g. `verdict_flip_shared_body`, `bulk_green_weight`, `no_coffee_complete_runs` |
| `family` | the signal family (section 3.1); two flags agree "independently" only across families |
| `proposed_action` | an action from 2.3 |
| `inputs` | which fields the signal read: `title`, `type`, `tags`, `body`, `variants`, `prices`, `catalog`, `crawl`, `registry` |
| `evidence` | JSON with references, not copies: input_hash, body_hash and how many products share it, the verdict's model and definition, variant grams, fetch statuses by week, counts. Description text stays in `bodies/` |
| `public_rows` | how many public rows the action would change (the queue's sort key) |
| `code_version` | the image that raised it |

A flag is *open* while the latest build raises it. When a later build stops raising it (the evidence went away),
it is *expired*, with no row written: expiry is derived.

### 2.2 `decisions`: what a person (or a pre-approved rule) decided

`decisions/decision_id=<id>__<event time>.parquet`, one file per event, like `run_overrides`.

| Column | Meaning |
|---|---|
| `decision_id`, `event` | the case and this event: `checked`, `approved`, `rejected`, `reverted` |
| `event_at` | strictly increasing per subject (the `add_override` rule: one second after the last if needed) |
| `subject_kind`, `subject_id`, `action` | as in 2.1 |
| `params` | the action's arguments: `batch_id` for `use_batch`, a crawl method for `set_crawl_method` |
| `flag_ids` | the flags it answers (empty for a decision made from the CLI or from scratch) |
| `subject_hash` | the subject's input_hash (product) or catalog hash (roaster) when decided |
| `evidence` | a frozen copy of the flags' evidence at decision time |
| `live_check` | for `checked`: URL, fetched_at, HTTP status, and the owner's finding (`confirms`, `contradicts`, `cannot_tell`) and note |
| `actor` | the owner's verified email (IAP), `cli:<user>`, or `rule:<name>` for a pre-approved rule (3.4) |
| `reason` | required, as for overrides: the audit trail is the point |
| `expires_at` | optional: a re-check date (a blocked store, a not-a-coffee-seller roaster) |

### 2.3 Subjects and actions

| Subject | Actions | Effect |
|---|---|---|
| product | `exclude`, `include` | out of / into the published catalog, whatever the classifier says |
| variant | `exclude_variant` | a size dropped from prices and bag sizes (a 60 kg sack inside a retail listing, a misread weight) |
| roaster | `not_a_coffee_seller`, `not_a_roaster_site`, `restore` | a status; not crawled weekly; out of `public_roaster` |
| batch | `accept`, `reject`, `use_batch` | today's overrides, unchanged in meaning |
| store | `set_crawl_method`, `mark_blocked`, `unblock` | how the store is crawled (section 3.3); never removes data |

### 2.4 The state machine

```
            flag raised                approved (owner; removals: readiness rule, 3.2)
  (none) ───────────────▶ flagged ─────────────────────────▶ approved ──first served release──▶ applied
                            │  │                               │                                  │
     flag no longer raised  │  │ rejected (owner, reason)      └──────────── reverted ◀───────────┘
                            ▼  ▼                                    (owner, reason: back to the classifier)
                        expired  rejected ── re-flagged only when the evidence changes (new family, new hash)
```

- `flagged`, `expired` and `applied` are derived, never written. `applied` is the first served release whose build
  record lists the decision (`builds`, `kind = 'decision'`); `serving_log` already records what was served.
- Every state has a way out. `reverted` is a new event; nothing is undone by deleting.
- An approved decision whose subject later changes (a new `subject_hash`: the roaster rewrote the listing) stays
  applied and raises a `subject_changed` flag, so the owner re-reads it.
- An `expires_at` in the past turns an applied decision back into a flag, never into a silent revert.

### 2.5 What it replaces or extends

| Today | With decisions |
|---|---|
| `run_overrides` | Kept and still read: old files are immutable. `stg_run_overrides` reads both: overrides, plus approved `batch` decisions, by time. `coffeedata override` writes a decision from then on (actor `cli:…`) |
| `config/roasters.csv` | Stays the curated seed, in git. It is a list of candidates, not a verdict on any of them |
| registry opt-out (issue form) | Stays, and needs no second signal: it is the roaster's own request. It shows in the admin's history as `rule:opt_out` |
| `NOT_A_ROASTER_SITE` | Stays in code: it is a rule about platforms (Yelp, Toast, Square sites), tested and the same for everyone. A single business that isn't a roaster becomes a `not_a_roaster_site` or `not_a_coffee_seller` decision instead of another line in the tuple |
| verdicts | Unchanged. A decision is a correction on top, never an edit of a verdict |

### 2.6 Who owns which contract, and how a decision changes them

The owner owns every contract (the public contract's `team` block); the table says which component produces each
one and where it is changed.

| Contract | Producer → consumers | Changed by | What a decision does to it |
|---|---|---|---|
| Coffee definition (`coffee_definition.md`, GUIDE.md) + rules | git → verdicts, `stg_products`, enrichment, registry | PR + evaluation (CLAUDE.md). A definition edit re-verdicts every version, so it is a budget decision too | Nothing. Decisions never edit it. A run of similar decisions is the evidence for a PR (e.g. 22 sacks → "green, also by the sack") |
| Public catalog (ODCS 2.3.0) | build → dataset, Snowflake, site | contract first, then dbt (`tests/test_contracts.py`) | No column change. Once, a **minor** version: a quality rule `decisions_applied` (every approved exclude is absent, every include present) and the release-notes rule. Then each decision changes rows, not the contract |
| Internal raw / manifest / enrichment | crawl, jobs, enrichment → dbt | `raw_contracts.py`, `make contracts` | Nothing, except `set_crawl_method`, which records the method in `fetches` (a raw minor version if it needs a column) |
| **New: internal `curation` 1.0.0** | admin, CLI, build → dbt, registry, ingest | generated from `lake.SCHEMAS` like the others | Holds `flags`, `decisions`, and the three tables no contract covers today: `registry`, `verdicts`, `run_overrides` |

## 3. Flag sources and the multiple-signals rule

### 3.1 Signal families

A family is one source of evidence. Two signals from one family are one signal.

| Family | Signals (rule names) | Reads |
|---|---|---|
| `rules` | `classify.excluded` / `is_coffee` disagree with the outcome | title, type, tags, body, variants |
| `verdict` | Claude says `not_coffee` / `coffee` / `unsure` (`verdict_flip`, `unsure_public`) | title, type, tags, variants, body |
| `listing` | product type or tags name another category; variant weight ≥ 20 kg (`bulk_green_weight`); price per 100 g outlier (#111 rule 7) | type, tags, variants, prices |
| `catalog` | the body_hash is shared by ≥ 3 products of the roaster with different titles (`shared_body`: the text says nothing about any one product); a roaster's churn (`roaster_churn`: it loses ≥ 25% of its public coffees in one build) | catalog |
| `roaster` | the last 2 complete, accepted fetches have products and 0 coffees (`no_coffee_complete_runs`); most product types are one non-coffee category | catalog, registry |
| `crawl` | coverage gaps (3.3), fetch health (#93), `partial`, challenge streak (#110) | crawl |
| `person` | the owner's live check of the store (`checked`, `confirms`) | the live store |

### 3.2 The rule, stated precisely

- **A removal** is `exclude`, `exclude_variant`, `not_a_coffee_seller`, `not_a_roaster_site` or `reject` of a batch
  whose rows are public. A removal is **ready** for approval only when one of these holds:
  1. open flags from **at least two families** propose it, and at least one of them did not read `body` when the
     subject's body is `shared_body`; or
  2. one open flag proposes it and a `checked` event with finding `confirms` is on the same decision.
- **No coverage flag is open** on the subject's roaster. Missing data is fixed, not removed (3.3).
- **Then the owner approves.** A ready removal still waits for a person; the admin only enables the button.
- **A restore or addition** (`include`, `restore`, `revert`) needs one flag or the owner's reason, and the owner's
  approval. Putting a coffee back can't make the catalog lose anything.
- **Wholesale rows, opt-outs and the release gates** are unchanged.

**The classifier's own weekly output.** Read literally, "nothing is removed on a single signal" would hold every
coffee the classifier drops, and the verdict backfill dropped 3,765 at once (#109). One person can't review that,
and the sealed set measured the verdicts better than the rules (98.27% against 94.05%). So this design keeps the
classifier as the measured, automatic decision for each product, and adds **one automated guard** in its place:
a public coffee whose only removal signal read a `shared_body` description, or that belongs to a `roaster_churn`
roaster, stays published for that build and is flagged. It is a dbt rule, not a manual hold, and it starts as
warn-only until it is measured (3.5). Whether that is what the owner meant is open question 1.

### 3.3 The coverage signal: fix, don't remove

A roaster we read badly looks like a roaster with nothing to sell. Coverage flags propose `set_crawl_method` or a
code fix, and block every removal for that roaster while open.

| Signal | Detected from | Proposed action |
|---|---|---|
| `squarespace_one_collection` | the adapter reads the busiest sitemap collection and stops at the first with items (`if seen: break`, `adapters/squarespace.py`); flag when the sitemap lists other shop collections with product URLs we never read | code fix for every Squarespace store (all collections, deduplicated), measured on `eval/crawl` |
| `redirect_loop` | `fetches.error` is `TooManyRedirects` for our UA two weeks in a row while the probe reached the site | `set_crawl_method` (sitemap pages, or the secondary crawler #67) |
| `feed_forbidden` | 403 on the product feed (`/products.json`, the Woo Store API) while robots.txt and the home page answer 200 | `set_crawl_method = secondary` (#67, after its held-out score) |
| `sitemap_gap` | sitemap product URLs ≥ 1.25 × products read, on a complete fetch | investigate |
| `challenge_streak` | the same challenge (202, `sg-captcha`, `cf-mitigated`) for N consecutive weeks (N = 6, a variable) | `mark_blocked` + `set_crawl_method` (#110, January 2027) |

202 and 429 stay ordinary failed fetches: retried every pass and every week, never a flag before the streak.

### 3.4 Pre-approved rules

Some decisions the owner already made as rules. They are written with `actor = rule:<name>`, approved at once, and
shown in the history: an opt-out from the issue form, and `mark_blocked` after the challenge streak (#110). Neither
removes catalog data by itself. Anything else that removes data needs the owner.

### 3.5 Measured, not argued

Each automatic flag rule is a classifier of its own, so CLAUDE.md applies: a roaster-grouped, salted split, two
labelers and a third, Wilson intervals, the sealed test scored once. Until a rule is scored, its flags are raised but
never count toward readiness on their own; a live check does. Approved decisions are person-made labels, which the
labeling sets lack today: they go to the **train** set and the regression fixtures, never to a sealed test set
(and the eval scores the classifier before decisions, not after).

### 3.6 The worked examples

| Case (this week) | Flags raised | Ready? | Outcome |
|---|---|---|---|
| 16 Stivers Coffee single-origins removed (the same pasted "Sample Pack" text in each description) | `verdict_flip` (verdict), `shared_body` (catalog), `roaster_churn` (catalog); rules say coffee | The guard keeps them published and flagged from the next build. Restoring is one `include` per product, grouped | Owner bulk-approves 16 `include`s with one reason. Their body_hash is a regression fixture for the guard |
| 3 drink mixes added | rules say not coffee, verdict `coffee` (verdict flip, `rules` family); `listing` if the type says mix | Two families, or one plus a live check | `exclude` ×3. Three cases are a unit test, not a definition change |
| 5 businesses with no coffee for sale (roaster parts, a nut roastery, a bakery, a bookstore, a caffeine-free substitute) | `no_coffee_complete_runs` (roaster) + the product-type mix (roaster) are one family; a live check makes it ready | After a `checked: confirms` | `not_a_coffee_seller`, `expires_at` +6 months; out of the weekly crawl; re-probed then |
| 6 real roasters we fail to crawl | coverage flags (3.3); all removal flags on them blocked | Not a removal | Squarespace: one code fix; the redirect loop and the 403 feed: `set_crawl_method`. The list feeds #93's fetch-health page |
| 22 green-coffee sacks still public (e.g. copantrade.com "BRAZIL Coopama MTGB Sc 14/16 - 60 KG BAGS") | `bulk_green_weight` (listing) + `rules`/`verdict` where they agree, or a live check | Two families, or one plus a live check | `exclude` ×22 grouped by roaster. They are the evidence for a definition PR ("green, by the bag or the sack") only if the owner accepts a full re-verdict (open question 6) |
| Unsure verdicts (4,177 version rows) | `unsure_public` only where the product is public **and** a second family disagrees with the rules' answer | Rarely | The rest go to the labeling queue, not the review queue: one person can't read 4,000 |

## 4. How dbt applies approved decisions

Following the project's layers (staging → intermediate → marts) and extending models before adding new ones:

1. **`stg_decisions`** (staging): the latest event per `decision_id` with `event_at <= var('decisions_as_of')`;
   effective when that event is `approved`. `stg_flags` reads the latest build's flags.
2. **The one coffee decision stays one.** `products.decide(item, label, decision)` takes the product's effective
   decision, so `stg_products`, the enrichment job (an excluded product is never sent to Claude) and the registry
   agree, as DESIGN §3 requires. `stg_products` gains `classifier_is_coffee` and `decision_id`.
3. **Soft, in the internal layer.** A new `int_catalog_decided` keeps *every* candidate row: classifier output, the
   decision id and action that changed it, and what it would have been. `dim_coffee` reads it and keeps its current
   meaning (published coffees only), so the app, MCP and site need no new filter. Nothing is lost: the excluded row
   and its decision id stay in `int_catalog_decided`, which is exported to the private `marts/` for the admin.
4. **Roaster status.** `dim_roaster` and `registry.STATE_SQL` both gain `not_a_coffee_seller` (and the
   `test_python_and_dbt_agree_on_roaster_status` test with them); `select_states` skips it. `public_roaster` needs
   no enum change: such a roaster has no coffee in `dim_coffee`, so it is already left out.
5. **Batches** keep flowing through `stg_run_walk` as today; only `stg_run_overrides` reads one more source.
6. **Public tables filter, and never carry decision ids.** Their columns don't change.

**Reproducible.** `coffeedata build` sets `decisions_as_of` to its start time and records every applied decision in
`builds` (`kind = 'decision'`, `unique_id = decision_id`). Rebuilding an old release with the same cutoff gives the
same rows, so `rollback` and `build-diff` keep working. A decision takes effect at the next build, never on a
served release.

**Tested.** dbt unit tests (mocked inputs, expected rows) for: an exclude, an include over a verdict, a revert, a
decision past the cutoff (ignored), an expired one, a `use_batch` decision beside an old override. Singular tests:
`decisions_applied` (error) and `flags_have_evidence` (warn).

**Explained in release notes.** #111's `release_changes` gives every changed public row a reason; decisions add
the reason `decision:<action>` with the decision id. `coffeedata build` prints `release notes:` counts by reason
(e.g. "removed 22: decision exclude, green coffee; restored 16: decision include"), the build record keeps them,
and the HF `manifest.json` and the site's catalog page show the counts by reason, not the subjects (open
question 4). `release_changes_explained` stays a warning on `unexplained`.

## 5. admin.registry.coffee

### 5.1 Pages and flows

| Page | What it shows | Actions |
|---|---|---|
| **Queue** (home) | Open flags grouped by subject and action, then by roaster and rule; sorted by `public_rows`, then weeks open. Each row: subject, proposed action, one chip per family, "ready" or "needs a live check", weeks open | open, select a group |
| **Case** | Subject header with the store link. The signals and their evidence: rules' answer, verdict (model, definition hash), title, type, tags, sizes and prices, the description (private, shown only here), how many products share its text, the roaster's other products, 8 weeks of fetch statuses. The proposed effect: rows that leave or return | record a live check; approve or reject (reason required); a removal's approve is disabled until ready, with the reason why |
| **Group** | One rule × one roaster (16 Stivers, 22 sacks) | approve or reject all, one reason, one event per subject, one lake commit |
| **History** | Every decision, newest first, by subject, actor or action, with the release that applied it | revert (reason required) |
| **Coverage** | Roasters with open coverage flags and the proposed method | approve `set_crawl_method` |
| **Contracts** | Read-only: the definition hash and the verdict model, contract versions, `NOT_A_ROASTER_SITE`, the gate thresholds | "propose a change" opens a GitHub issue or PR draft |

The live check: the owner opens the store link in their own browser and records what they saw. A server-side
fetch through the bot's `PoliteClient` (robots.txt, the UA, the host spacing) is a convenience, not the check
(open question 8).

### 5.2 Reads and writes: the lake, no new database

- **Reads.** At start and after each build: the private `marts/` (with `int_catalog_decided`), the latest `flags`
  file, all of `decisions/` (small), and the bodies of flagged products only (DuckDB over `hf://` with a
  `body_hash` filter). That is tens of MB; in memory on one instance.
- **Writes.** Decision files only, sent the way `add_override` sends an override: `HfApi.upload_file`, one commit per
  action, one for a group. The admin never writes anything else, never builds, never serves.
- **Why not a small database (Cloud SQL, Firestore, SQLite on a volume).** The build must read decisions to be
  reproducible, and it reads the lake. A second store needs a sync, a backup and a second audit trail, for a few
  hundred rows a year. Cloud SQL alone would cost more than the whole stack.
- **Two writers** (admin and CLI) are safe: one person, append-only files, `event_at` strictly increasing per
  subject, latest event wins.

### 5.3 Hosting: its own Cloud Run service, from the app's image

| | Inside the existing app (app.registry.coffee) | **Separate service (recommended)** |
|---|---|---|
| Code reuse | full | full: same image, another entry point (`app.admin`) |
| Access | the service is public (`allUsers` invoker); admin routes would rely on the app's sign-in | no `allUsers`; IAP in front, so nothing reaches the code unauthenticated |
| Blast radius | an admin bug or memory spike takes down shot logging (one instance, 1 GiB, state in memory) | separate; the shot logger is untouched |
| Secrets | one HF token for both | its own fine-grained token and service account; no Anthropic key at all |
| Terraform | edits the live app's resources | additive: a new service, service account, IAM binding, domain mapping |

Terraform (for a later PR, reviewed as a plan by the owner): `google_cloud_run_v2_service.admin` (min 0, max 1,
`iap_enabled = true`), `google_service_account.admin`, `roles/iap.httpsResourceAccessor` for the owner's Google
account only, a secret `ADMIN_HF_TOKEN`, a domain mapping for `admin.registry.coffee` (CNAME `admin` →
`ghs.googlehosted.com`, as for `app`). Unknowns to check before the PR, not assumptions: that IAP on Cloud Run works
with a domain mapping in this project (if not, use the `run.app` URL; a load balancer costs about $18 a month,
more than everything else together), and the provider version that has `iap_enabled`.

### 5.4 Auth: IAP, plus a check in the code

The repo is public, so the code's routes are known; the protection can't be obscurity.
- **IAP** puts Google sign-in, with the account's 2-step verification, in front of the service. Free.
- **In the code**, every request must carry a valid `X-Goog-IAP-JWT-Assertion` (signature, audience) whose email is
  `ADMIN_EMAIL`; otherwise 403. It fails closed when the header is missing, so a misconfigured service exposes nothing.
- **Writes** keep the app's CSRF rule (`X-Requested-With` plus `Sec-Fetch-Site` / `Origin`).
- **Locally** (`make admin`), a stub like `auth.LocalOwner` that refuses to start when `hosted()`.
- **Why not the app's sign-in alone:** it is a password with no second factor, sessions last 90 days, and it has
  members. That is right for a shot log, and too little for the thing that edits the public dataset.

### 5.5 Audit trail, observability, cost

- **Audit.** Three records that agree: decision files (who, when, why, the evidence frozen), the HF dataset's
  commit history (one commit per action), and `builds` + `serving_log` (which release applied it). Cloud Run
  request logs (30 days) add the IAP identity on every request.
- **Logs.** One JSON line per write with `decision_id`, `action`, `subject_kind` and the outcome; no description
  text, no tokens. An ERROR log on a failed lake write, which the existing error alert policy pattern can email.
- **Cost.** Cloud Run scales to zero and a few hundred requests a week sit inside the free tier; IAP, the domain
  mapping and the HF dataset are free. About **$0 a month** on top of the current $9–14. The build's flag model
  adds seconds, not dollars. No model API is called by the admin.

### 5.6 The UI: the app's design, not a new one

- **Reuse, don't restyle.** `app/static/app.css` (Bench Scale: the tokens, Archivo and Newsreader, the type scale,
  spacing and radii in `docs/app/DESIGN.md`), the same light and dark themes, and its components: list link, chips,
  status text, spec sheet, notice. The header matches the site's nav. No new colours: `--danger` only on a removal's
  approve, `--balanced` for "ready".
- **Familiar patterns for a review tool** (Jakob's law): a queue like an inbox or a PR review: a list, a detail
  page, approve and reject at the bottom, history. Keyboard: `j`/`k` to move, `a`/`r` to act (with the reason field).
- **No dashboard.** No KPI tiles or charts; the queue is the page. Counts are plain text above it.
- **States are visible** (the state machine): each state has one label and one look; a disabled approve says why;
  every applied decision shows its revert.
- **Accessible:** text labels on every chip and button (never colour alone), real buttons and form labels, focus
  visible, the 4.5:1 text contrast the app's tokens already meet. Desktop-first, usable on a phone.

## 6. Alternatives

| Option | Review with evidence | Approve / reject | Audit trail | Cost | One person | Verdict |
|---|---|---|---|---|---|---|
| **Admin app (recommended)** | Yes, including private descriptions | Yes, with readiness enforced | Lake + HF commits + builds | ≈ $0 | Built for it | Build it, after the data model, locally first |
| GitHub issue forms | Poor: the repo is **public**, so evidence (descriptions are the roasters' and stay private) and labels like "not a coffee seller" would be public; no grouping of 16 or 22 items | A label or a comment, parsed by a workflow | Excellent (issues) | $0 | OK for a few a week | Keep it for adding and opting out roasters, and for public reports. Not the queue |
| Grafana / Metabase | Good for reading tables and trends | No native write-back; needs a plugin or a form to an API anyway | Weak | Grafana Cloud free tier; self-hosted needs a VM | Fine | Not for decisions. Possibly later for crawl health |
| Retool / Appsmith | Fast to build tables and buttons | Yes, via an API we'd still write | The tool's own, plus ours | Retool's free tier; Appsmith self-hosted needs a VM | Fine | The private evidence and the HF token would sit with a third party, and it can't read Parquet on HF without an API we'd build anyway. Not worth it here |
| CLI only (`coffeedata decide`, `flags`) | Weak: a terminal is a bad place to read a description and 8 weeks of fetches for 22 items | Yes | The same lake rows | $0 | Works this week; not at the next 3,000-item wave | Build it anyway, as phase 1's writer and for scripts. Not the only UI |

**Is the owner's choice right?** Yes for reviewing items: nothing else shows private evidence to one person with an
audit trail at no cost. It would be wrong in two ways that are easy to slip into: building the hosted app before
the `flags` / `decisions` model exists (then the app *is* the model, and the build can't reproduce it), and letting
the admin edit the coffee definition, rules or contracts (that skips the evaluation CLAUDE.md requires and, for the
definition, re-verdicts every product). The admin should show those, and link to a PR.

## 7. Phasing

**Phase 1: a queue in about a week (no infrastructure).**
- `flags` and `decisions` in `lake.SCHEMAS`; the `curation` contract; `lake()` globs; `coffeedata decide`
  (approve / reject / revert / check) and `coffeedata override` writing decisions.
- `mart_flags` with the cheapest rules that cover this week: `verdict_flip`, `shared_body`, `roaster_churn`,
  `bulk_green_weight`, `no_coffee_complete_runs`, `redirect_loop`, `feed_forbidden`, `squarespace_one_collection`.
  Raised and shown; readiness by live check until each rule is measured.
- dbt: `stg_decisions`, `products.decide` with decisions, `int_catalog_decided`, `not_a_coffee_seller`, the unit
  tests, `decisions_applied`.
- `make admin`: Queue, Case, Group and History pages on localhost, against the remote lake with the owner's token.
- This week's cases decided through it. CLAUDE.md step 9: 41 products (16 + 3 + 22) plus whatever the 5
  roasters still have public, each listed and checked by hand. Count those first: over 50 means the full process.

**Phase 2: hosted.** The Cloud Run service, IAP, the JWT check, the domain mapping, the secret, an alert on failed
writes. Coverage page. Release notes on the site and in `manifest.json`.

**Phase 3: automation.** Each flag rule scored (CLAUDE.md) so two scored families make a removal ready without a
live check; the shared-body guard moved from warn to holding; `set_crawl_method` read by `select_states`; the
Squarespace fix; #110's streak (January 2027); the Contracts page.

**Later, only if needed:** attribute corrections (origin, process), merging near-duplicate roasters (#111 rule 12).

## 8. Risks

| Risk | Mitigation |
|---|---|
| A bad bulk decision removes many coffees | Decisions apply only at a build, behind the gates; the volume check and `release_changes` see it; one revert per decision, and `rollback` serves the last release meanwhile |
| The queue outgrows one person | Sort by public impact; only multi-family or ready flags by default; unsure verdicts go to labeling; flags expire when their evidence goes |
| Heuristic flags are trusted before they're measured | Unscored rules never make a removal ready alone (3.5) |
| Private text leaks | IAP, the JWT check, no `allUsers`; descriptions are never logged or put in flags; the public tables never carry decision data (`public_tables_are_facts_only` stays) |
| Decisions and the classifier drift apart | A decision the classifier now agrees with shows "redundant"; a changed subject re-flags (2.4) |
| HF commit limits on bulk actions | One commit per group, not per item |
| IAP doesn't fit the domain mapping | Use the `run.app` URL; never a paid load balancer for one user |

## 9. Open questions for the owner

1. Does "nothing is removed on a single signal" cover the classifier's weekly output (the backfill's 3,765), or only
   removals made through decisions? The proposal: decisions, plus the automated shared-body and churn guard.
2. Should a restore (`include`) need your approval, or apply on one flag? Proposed: your approval, one signal enough.
3. A "not a coffee seller": stop crawling it until a re-check (proposed: 6 months), or keep crawling monthly?
4. Release notes in public: counts by reason only (proposed), or the named roasters and coffees as well?
5. Which Google account signs in, and is the `run.app` URL acceptable if IAP can't sit behind the domain mapping?
6. Green coffee by the sack: fix by decisions and a weight rule, or add it to the definition and pay for a full
   re-verdict (every version becomes pending)?
7. Unsure verdicts: send them to labeling (proposed), or to the review queue?
8. Live checks: your browser only, or also a server-side fetch by the bot?

## Skills used

- dbt and data quality (sections 1–4): dbt-labs `using-dbt-for-analytics-engineering` (follow the existing layers,
  extend a model before adding one, read the data first: the counts above come from a local read-only lake copy),
  `adding-dbt-unit-test` (unit tests for the decision logic and the bug cases), wshobson `dbt-transformation-patterns`
  (staging → intermediate → marts), `data-quality-frameworks` (contracts versioned, warn before block, dynamic
  baselines), MotherDuck `motherduck-model-data` (grain and types decided before modeling).
- DevOps (section 5): `terraform-skill` (blast radius: an additive service; secrets apart; a reviewed plan before
  any apply), wshobson `deployment-pipeline-design` (automated gates, rollback, an approval step), and
  `python-observability` (structured logs with the decision id, bounded fields, no secrets).
- Design (5.6): MengTo `no-ai-design-slop` (keep the existing direction; no dashboard filler), Owl-Listener
  `jakobs-law`, `state-machine` and `accessibility-audit`.
