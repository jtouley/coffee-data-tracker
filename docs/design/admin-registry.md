# admin.registry.coffee: change it, see the draft, publish or discard

Status: proposal (2026-11-04), revised after the review on #112 and three owner decisions. Nothing here is built.
Owner: jtouley.

**The owner's direction.** "It builds a dbt version draft that checks the outcomes, but doesn't publish unless I
allow." Also: "CLI and APIs should all be built as part of admin.registry.coffee", and "rules and excludes need
to be config-driven, code-backed, from the admin panel."

**The design in five lines.**
1. **Every change made in the admin produces a draft build.** A change is a batch of item decisions ("exclude
   product X") or a rule-config edit. The draft is the normal `coffeedata build`, with every gate, recorded but
   **not served**.
2. **The admin shows the draft's outcome against the served release.** You see the gate results, `build-diff`, and
   #111's release-against-release changes with a reason for each row.
3. **Publish** serves that build with the existing machinery (`quality.serve`, as `rollback --to` does), then the
   normal export and site. **Discard** drops it.
4. **The weekly scheduled release stays automatic**, gated by the existing tests. Drafts are only for changes made in
   the admin.
5. **One control plane.** The admin's HTTP API is the only writer; the UI and `coffeedata decide` are its clients.
   It runs as its own Cloud Run service behind IAP, as the security review recommends (5.3).

## 1. Today (checked on `origin/main`, 28a1b05)

| Piece | Where | What it means here |
|---|---|---|
| Build | `cli.cmd_build` | dbt builds and tests, then `quality.record_build` writes `builds/` and an immutable `releases/…/build=<id>/` for **every** build, passing or not; only then `quality.serve` if it passed |
| Serve, rollback | `quality.serve`, `quality.rollback(to=…)`, `cmd_rollback` | serve any recorded passing build, log it in `serving_log`, then export the public tables and the site |
| Diff | `quality.build_diff`, `coffeedata build-diff --old --new` | models, tests, counts, metrics, and the coffees added, delisted or out of stock |
| Release notes | #111 (open) `release_changes` | every public row added, removed or changed against the served release, with a reason |
| Item overrides | lake `run_overrides/` (roaster-week `accept` / `reject` / `use_batch`), `coffeedata override` | the only kind today. There is no product or variant override and no "sells no coffee" status |
| Rules | `classify.py`, `products.decide`; thresholds in code | `dim_coffee` drops a product as soon as its latest version isn't coffee. That is how the 16 Stivers coffees went |

So a draft needs **one new flag on an existing command**: `coffeedata build --draft <id>` records the build and
skips `serve`. Publish is `serve` on that build. Nothing else needs a separate replay system.

## 2. The flow

```
 admin: edit (decisions and/or rules)  ──▶  draft build (all gates, recorded, not served)
                                                │
                     draft page: gates · build-diff · #111 changes by reason, vs the served release
                                                │
                         Publish ──▶ commit the change ──▶ serve the draft ──▶ export + site
                         Discard ──▶ mark the draft discarded (its build stays recorded, as failed builds do)
```

- **A draft** has an id, the proposed decision events and an optional proposed rule-config version. The draft build
  reads approved decisions plus **this draft's** proposed ones, and the active config or the proposed one.
- **Publish:**
  1. checks that the draft build passed;
  2. checks that the served release is still the one the draft was compared with (its `baseline_build`). A weekly
     release in between means the draft must be rebuilt first;
  3. checks that any evaluation CLAUDE.md requires is attached (section 4);
  4. writes the `approved` events and the active-config pointer in one commit;
  5. serves the draft build with `serve(…, action="publish")`, then exports the public tables and the site, as
     `cmd_rollback` does.

  Publish is idempotent: a retry after a failed serve re-serves the same build.
- **Discard** writes `discarded` for the draft's events. Nothing is served, and nothing is deleted.
- **The weekly build** reads only approved decisions and the active config. It serves automatically when its gates
  pass, as today, and never picks up an open draft.
- **Where the draft build runs.**
  - Phase 1: on the owner's laptop. `make admin` runs `coffeedata build --draft` with a short-expiry HF token minted
    for the session and revoked after, never a standing one. It takes minutes, the weekly build's time.
  - Phase 2: the admin starts the existing Cloud Run `build` job with `--draft <id>`. The admin service itself never
    writes the lake.

## 3. The data: decisions and rule config

### 3.1 Decisions (per item, data)

`decisions/<decision_id>/<seq>-<event>.parquet` is kept in a **private curation dataset** (`Lake.curation()`,
`COFFEEDATA_CURATION`), like `Lake.photos()` and `Lake.users()`. Hugging Face fine-grained tokens are scoped per
repo, so the admin's token gets curation write and lake read, and it can't touch `verdicts/` or `snapshots/`.

| Column | Meaning |
|---|---|
| `decision_id`, `seq`, `event` | the case; events `checked`, `proposed`, `approved`, `discarded`, `reverted`. `decision_id` + `seq` is the API's idempotency key, so a retry rewrites the same file |
| `draft_id` | the draft that proposed it |
| `subject_kind`, `subject_id`, `action` | product (`coffee_id`): `exclude` / `include`; variant: `exclude_variant`; batch (roaster, run_date): `accept` / `reject` / `use_batch` (the existing overrides). Roaster `not_a_coffee_seller` comes later |
| `flag_ids`, `evidence`, `live_check` | what prompted it, frozen; a live check's URL, time and finding |
| `actor`, `client`, `reason` | the authenticated identity, `ui` or `cli`, and a required reason |

- Writes commit with `parent_commit` set to the revision the client read, so a stale tab fails loudly.
- `run_overrides` stays readable, and `coffeedata override` becomes an API client.

### 3.2 Rule config (how things are flagged, held and ordered)

Code implements the rule types; a versioned YAML with a JSON Schema sets their parameters. The schema's enum of rule
types is generated from the code, so a config naming a type the image lacks fails validation, and its draft build
fails.

```yaml
version: 1.0.0
rules:
  - {id: verdict_flip, family: classifier, polarity: {added: supports, removed: restore}}
  - {id: shared_body, family: catalog, polarity: doubts, params: {min_products: 3}}
  - {id: bulk_green_weight, family: listing, polarity: supports, proposes: exclude, params: {min_grams: 20000}}
readiness: {removal: {supports_min: 1, live_check: required, blocked_by: [doubts, coverage]}}
guard: {mode: hold, when_doubted_by: [shared_body], max_held: 200, max_weeks: 4}
precedence: [roaster, product, variant]
action_groups: {visibility: [exclude, include, exclude_variant], batch: [accept, reject, use_batch]}
```

**Where it lives: the curation dataset (recommended).** Versions are immutable files `rules/<version>.yaml`, and
Publish moves the active pointer.

| | Curation dataset (recommended) | Git, through PRs opened by the admin |
|---|---|---|
| Fits the draft flow | yes: a draft build reads the proposed version directly | needs a branch checkout for the draft build, and a merge on Publish |
| Checks | the draft build is the check, with every gate | CI on the PR, which repeats the draft build |
| Credentials | none beyond the curation token | a GitHub token on the admin |
| Visible to the public | no; the build records the version, and the public contract can cite it | yes |

The draft build already does what a PR's checks would. Git would add a token and a second review step for one
person. The rule *types* stay in git, as code with tests. The coffee definition, the crawl methods and the registry
seed stay in git too; the admin shows them read-only.

### 3.3 The review's four MAJORs, as config, code and tests

| Finding | Config | Enforced by code | dbt test |
|---|---|---|---|
| M1: doubt signals counted as votes to remove | `polarity` per rule (required: `supports` / `doubts` / `restore`); `readiness.removal` | readiness counts only `supports`; an open `doubts` or coverage flag blocks; Phase 1 readiness is one flag plus a live check | `ready_removals_have_no_doubts` (error) |
| M2: the classifier's removals bypass review | `guard.mode` (default `hold`), `max_held`, `max_weeks` | `int_catalog_decided` keeps a doubted removal's **last version that was coffee** | `classifier_guard` (error over `max_held`) |
| M3: no single effective decision per subject | `precedence`, `action_groups` | the latest `approved` / `reverted` event per (subject, group) wins, in commit order; roaster > product > variant | `unique` on `int_effective_decisions`; three conflict unit tests; `decision_orphaned` (warn: a platform move changes `coffee_id`) |
| M4: an audit trail behind a token that can rewrite the lake | none (deployment) | the separate curation dataset; `Lake.curation()` refuses the lake's URL | a `terraform test` on the admin's secrets |

The guard runs in every build, the weekly one included. That is M2: the Stivers kind of removal is held
automatically, with a flag in the queue, instead of waiting for someone to notice.

## 4. The gates, and CLAUDE.md

- **A draft runs every existing gate unchanged:** completeness, ok rates, the volume check against the served
  release, the bands, the facts-only and contract tests. It adds three tests: `decisions_applied` (every effective
  exclude is absent and every include present), `classifier_guard`, and the unique test.
- **A failing draft can't be published**, because `serve` refuses a failed build. Never downgrade a failing test to
  pass a draft; find out why it fails.
- **CLAUDE.md for rule changes, in one line:** a config edit that changes how a rule decides (a polarity, a guard
  mode, a readiness rule, a new rule), or that changes more than 50 products on top of the field's running total,
  needs its evaluation attached (`eval/<rule>/` README, RESULTS and SHA256SUMS, at a commit on `main`) before Publish.
  Smaller edits take step 9's short path, and the draft's diff is the list to check by hand.
- **Reproducible.** Each build records its config version, the curation dataset's commit, its `draft_id` (if
  any) and every decision id it applied, in `builds`. A rebuild reads those, never a cutoff time.

## 5. The admin

### 5.1 Phase 1 pages

| Page | Shows | Actions |
|---|---|---|
| **Queue** | open flags by subject, then roaster and rule, sorted by public rows affected; one summary per row ("1 supports · 1 doubts · held"); held products | open a case; add it to the draft |
| **Case** | each flag with its polarity and evidence: rules' answer, the verdict, title, type, tags, sizes, prices, the description, how many share it, 8 weeks of fetches | live check; propose exclude or include with a reason (into the draft) |
| **Draft** | what's in it; build status; gate results by test; `build-diff`; #111's rows in and out per table with reasons; the public row count that leaves | build; **Publish**; **Discard** |

The Draft page is the point of the design. **Publish** is disabled until the build passed, the baseline is still
served and any required evaluation is attached, and it says which of these is missing. Its confirm names the
number of public rows that leave.

Every page defines its states:
- **empty:** "Nothing to review", as of build X;
- **loading:** "Reading build X…";
- **building:** shows the draft build's progress;
- **stale:** "the served release changed: rebuild the draft";
- **failed:** keeps the typed reason, retries with the same key, and says what was and wasn't recorded.

### 5.2 API, safety, hosting

- **The API is the only writer.** Its endpoints:
  - `GET /api/queue`, `GET /api/cases/{subject}`;
  - `POST /api/drafts`, `POST /api/drafts/{id}/events`, `POST /api/drafts/{id}/build`;
  - `POST /api/drafts/{id}/publish`, `POST /api/drafts/{id}/discard`.

  `coffeedata decide` is a thin client.
- **Untrusted text.** Titles, tags and descriptions render as escaped text, never `|safe`. The admin keeps
  `app/web.py`'s CSP and `Referrer-Policy: same-origin`, links only `https:` store URLs, and keeps the app's CSRF
  rule.
- **Hosting (Phase 2).** A separate Cloud Run service from the app's image (min 0, max 1), with no public access.
  - It has its own service account, and its own fine-grained HF token: **read** on the lake, **write** on the curation
    dataset, nothing else. It never shares the app's `HF_TOKEN`, and holds no Anthropic key.
  - It may run the `build` job with overrides, on that job only, for draft builds.
  - The Terraform is additive and applied by the owner from a reviewed plan. The domain mapping is
    `admin` → `ghs.googlehosted.com`.
  - `deploy.yml` deploys one `$SERVICE`: deploy both services from the same image in one job.
  - About **$0 a month** (plus about $50 once for a hardware key). The admin calls no model API.

### 5.3 Access (from the security review)

The owner holds the security review of 2026-11-04. It is not committed, because it lists open weaknesses in the live
app. Its recommendation, in short:
- **Sign-in is IAP.** In the code, every route but `/health` checks the IAP token and fails closed: the signature,
  `iss`, the pinned audience, and the email equal to `ADMIN_EMAIL`. There is a test for each failure, and no
  env-var bypass: the local stub is chosen by a `make admin` flag and refuses when hosted.
- **The app's password sign-in is not reused** for the admin, and there are no magic links or long-lived personal
  API keys.
- **The owner's Google account** uses two passkeys or hardware keys, with printed backup codes.
- **The CLI** calls the admin API through IAP with the owner's short-lived `gcloud` ID token. No long-lived HF token
  sits on the laptop. Break glass is a short-expiry HF token minted on huggingface.co when needed, then revoked.
- **Deploys:**
  - a ruleset on `main` (PR and checks, no force-push);
  - an `admin` GitHub environment the owner approves;
  - the admin's deploy credential bound to that environment;
  - a `terraform test` asserting no `allUsers` member and `iap_enabled`.
- **Alerts and limits:**
  - an email on every admin write, on every rejected IAP token, and on any IAM or service change to the admin;
  - write limits per identity;
  - a typed confirmation (the count) when a Publish or a group changes more than 50 public rows.
- **The repo stays public**: no control depends on hidden code.

**Open risk:** IAP on Cloud Run may require a Google Cloud organization (open question 3). If this project can't
use it, the fallback is the review's passkey-only sign-in:
- no password, two authenticators;
- 12-hour, revocable sessions;
- CLI device-flow tokens in the OS keychain.

### 5.4 Audit and UI

- **Audit.** One trail: decision events (the IAP email, which client, why), the curation commits, and `builds` and
  `serving_log` (Publish is a `publish` row). Builds record the curation revision they applied, so tampering with
  its history shows as a diff. A failed write logs at ERROR.
- **UI.** Bench Scale (`app/static/app.css`, `docs/app/DESIGN.md`), both themes, the existing components. No new
  colours (`--danger` only on Publish's confirm when rows leave), no dashboard tiles. A text label on every state;
  any keyboard shortcuts can be turned off (WCAG 2.1.4).

## 6. This week's cases

| Case | In the admin | The draft shows |
|---|---|---|
| 16 Stivers single-origins removed (the same pasted "Sample Pack" text) | `verdict_flip` restore + `shared_body` doubts; the guard already holds them in the weekly build | 16 `include`s: 0 rows out, `held:guard` → `decision:include` |
| 3 drink mixes added | a live check each, `exclude` ×3 | 3 rows out of `public_coffee`, reason `decision:exclude` |
| 5 businesses that sell no coffee | product `exclude`s after a live check (the roaster status comes later) | their rows out, by roaster |
| 6 roasters we fail to crawl | coverage flags block any removal; the fix is code or crawl config in git | nothing to publish |
| 22 green-coffee sacks (e.g. copantrade.com "… 60 KG BAGS") | `bulk_green_weight`; one live check on a sample covers the group | 22 rows out |
| Unsure verdicts (4,177 version rows) | none: they go to labeling | — |

## 7. Alternatives, briefly

- **Admin service (recommended).** It shows private evidence, runs drafts, and gives one audit trail at about $0.
- **Issue forms.** The repo is public, so the evidence would be too. Keep them for adding and opting out roasters.
- **A private repo of decision CSVs.** Good audit, but no evidence beside the item, and clumsy for 16 or 22 items.
- **Grafana or Retool.** Grafana can't write back. Retool puts the token and the private text with a third party.
- **A CLI that writes the lake directly.** That is a second write path; the CLI is an API client instead.

## 8. Phasing

**Phase 1: change it in the admin, see the draft's diff, publish or discard.**
- `coffeedata build --draft <id>` (record, don't serve) and `serve(action="publish")`;
- the curation dataset with `decisions/`;
- dbt applying `exclude`, `include` and `exclude_variant`, with the M3 tests and `decisions_applied`;
- the guard holding, in config 1.0.0, after its CLAUDE.md evaluation;
- the three rules;
- the API and `coffeedata decide`;
- Queue, Case and Draft on localhost.

It depends on #111 for the per-row reasons. Until #111 merges, the Draft page shows `build-diff`.

**Later, each only when needed.**
- Rule-config editing in the admin, through the same draft flow;
- hosting behind IAP, with the review's deploy rules and alerts;
- group approvals and History;
- `not_a_coffee_seller`;
- more rule types, each scored;
- public release-note counts;
- the runbook entries: a bad publish (roll back, then `revert`); a failed write; a lockout (the IAM binding fixed by
  the project Owner from a reviewed plan, or a short-expiry HF token for break glass); and the review's revoke order;
- extending the curation contract to `registry`, `verdicts` and `run_overrides`.

## 9. Open questions for the owner

1. **The guard's scope.** It holds a public coffee's classifier removal only when a shared description doubts it,
   capped at 200 products and 4 weeks. Should it hold every verdict-only removal instead? That would have held most
   of the backfill's 3,765.
2. **Restores.** Should `include` need your Publish like everything else? Proposed: yes.
3. **Can this project use IAP?** IAP on Cloud Run may need a Google Cloud organization. Is the project in one? If
   not, do you accept the review's fallback: passkey-only sign-in (no password, two authenticators, 12-hour
   revocable sessions) with CLI tokens in the OS keychain? Magic links and long-lived keys are ruled out either way.
4. **Where the rule config lives:** the curation dataset (proposed), or git through PRs the admin opens?
5. **Green coffee by the sack:** decisions plus the weight rule (proposed), or a definition change and a full
   re-verdict?
6. **Group checks:** is one live check on a sample enough for a group with the same evidence (proposed)?
7. **A draft made stale by the weekly release:** rebuild it automatically, or wait for you to click Build again?

## Skills used

- **dbt and data quality** (3, 4): dbt Labs `using-dbt-for-analytics-engineering`, `adding-dbt-unit-test`,
  `troubleshooting-dbt-job-errors`; wshobson `data-quality-frameworks`.
- **DevOps** (2, 5.2): `terraform-skill`, HashiCorp `terraform-test`; wshobson `python-resilience` (idempotent
  retries) and `deployment-pipeline-design` (gate, approve, promote, roll back).
- **Design** (5.1): MengTo `no-ai-design-slop`; Owl-Listener `state-machine` and `accessibility-audit`.
