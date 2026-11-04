# admin.registry.coffee: a review queue for data decisions

Status: proposal (2026-11-04), revised after the review on #112. Nothing here is built. Owner: jtouley.

**The ask.** The owner wants flags about registry.coffee's data raised automatically, backed by metadata, and
reviewed in one place before anything is removed: "Before we remove things, they need flagging [from] multiple
issues", "an admin ability to remove them, rather than how we have these datasets right now", and "what owns the
contracts of the coffees we're getting, how are they established, how do we edit them from the control plane?"

**The recommendation, in short.**
1. The core is a **data model, not a website**: pipeline-raised `flags` (each with a direction) and an append-only
   `decisions` log in its own **private curation dataset**, applied softly by dbt. Every UI reads and writes those.
2. The classifier's removals of public coffees are **held** by an automated guard from the first slice, so the
   Stivers kind of wrong removal can't happen again while the queue catches up.
3. Build the **admin app** as a small server-rendered service from the existing app's image, on its **own Cloud
   Run service behind IAP**. It is the only option that shows private evidence to one person, groups items, enforces
   readiness, and costs about nothing. Start on localhost; host it second.
4. The admin decides **items**. The coffee definition, rules, crawl methods and contracts stay in git, changed by
   PR and measured (CLAUDE.md).

## 1. Today (checked in the code on `origin/main`, 28a1b05)

| Piece | Where it lives | Notes |
|---|---|---|
| Coffee definition | `src/coffeedata/coffee_definition.md` (the verdict prompt; `verdict.DEFINITION_VERSION` is its sha256, 12 hex) and `eval/sealed/GUIDE.md` (labeling; same text, another title line). Green coffee is `not_coffee` in both | A change makes every product version pending for Claude again |
| Rules | `classify.excluded` / `is_coffee`; `products.decide` (the verdict wins unless the rules exclude outright), shared by `stg_products`, enrichment and the registry | Code |
| Verdicts | lake `verdicts/`. Local read-only copy after the 11-04 backfill: 41,315 coffee, 16,743 not_coffee, 4,177 unsure version rows; `unsure` falls back to the rules | `classify` |
| Registry | `config/roasters.csv` (3,203 rows, git) + lake `registry/` (discovery, the add-roaster issue form, opt-outs) | `stg_registry`, `registry.STATE_SQL` |
| Not a roaster | `registry.NOT_A_ROASTER_SITE`: 29 platform hosts, hard-coded | Code |
| Overrides | lake `run_overrides/`: roaster-week `accept` / `reject` / `use_batch`, a required reason; latest per roaster-week | `coffeedata override` only |
| Contracts | public ODCS 2.3.0 (team: jtouley, owner); internal raw 1.5.0, manifest 1.1.0, enrichment 1.1.0 | `registry`, `verdicts`, `run_overrides` are in no contract |

Missing, as the owner said: no product- or variant-level override, no "sells no coffee" status, no review queue.
And `dim_coffee` drops a product as soon as its latest version isn't coffee ("drops out entirely"), so one wrong
verdict removes it with no trace but the build diff. That is how the 16 Stivers coffees went.

## 2. The data model

### 2.1 Where it lives: a private curation dataset

`decisions/` goes in its own private Hugging Face dataset, `Lake.curation()` (`COFFEEDATA_CURATION`), the same
pattern as `Lake.photos()` and `Lake.users()`. HF fine-grained tokens are scoped per repo, not per path, so a token
that can write decisions into the lake could also rewrite `verdicts/` or `snapshots/`.

| Who | Lake | Curation |
|---|---|---|
| Admin (hosted) | read | write |
| CLI (`coffeedata decide`, the owner's laptop) | read | write |
| Build job | write (as today; it writes `flags/`) | read |

`flags/` stays in the lake: only the build writes it. Nothing in either dataset is edited or deleted.

### 2.2 `flags`: what the build noticed

`flags/run_date=<d>/build=<id>.parquet`, one file per build, from a dbt model `mart_flags` (one row per flag ×
build).

| Column | Meaning |
|---|---|
| `flag_id` | `sha256(subject_kind, subject_id, rule)[:16]`: stable across weeks, so "open 3 weeks" is a count |
| `evidence_hash` | hash of the rule's evidence signature (below); a rejected flag is raised again only when it changes |
| `build_id`, `run_date` | the build that raised it |
| `subject_kind`, `subject_id` | `product` (coffee_id), `variant` (coffee_id + variant_id), `roaster` (roaster_id) |
| `rule` | e.g. `verdict_flip`, `shared_body`, `bulk_green_weight` |
| `polarity` | `supports` (a removal), `doubts` (blocks a removal's readiness), `restore` (proposes putting a row back) |
| `proposed_action` | `exclude`, `exclude_variant`, `include`, `not_a_coffee_seller`; null for `doubts` |
| `evidence` | JSON references, not copies: input_hash, body_hash and its share count, verdict model and definition, grams, counts. Description text stays in `bodies/` |
| `public_rows` | public rows the action would change (the queue's sort key) |

A flag is *open* while the latest build raises it, and *expired* (derived, no row) once a build stops raising it.

### 2.3 `decisions`: what a person decided

`decisions/<decision_id>/<seq>-<event>.parquet` in the curation dataset, one file per event.

| Column | Meaning |
|---|---|
| `decision_id` | made once, when the owner opens the action (a UUID), and reused on every retry |
| `seq`, `event` | `1-checked`, `2-approved`, …; events are `checked`, `approved`, `rejected`, `reverted` |
| `event_at` | the writer's clock; for display. Order and reproducibility come from `seq` and recorded ids (4.4) |
| `subject_kind`, `subject_id`, `action` | as in 2.2, plus batch subjects (roaster_id, run_date) for `accept` / `reject` / `use_batch` |
| `params` | `batch_id` for `use_batch` |
| `flag_ids`, `evidence` | the flags answered and their evidence, frozen |
| `subject_hash` | the product's input_hash when decided |
| `live_check` | for `checked`: URL, time, the owner's finding (`confirms`, `contradicts`, `cannot_tell`), a note, and `covers` (the group it vouches for, 3.3) |
| `actor`, `reason` | the IAP-verified email or `cli:<user>`; a reason is required |

**Writes are idempotent.** The path is built once per action, so a retried upload rewrites the same file instead of
adding a second event. Each write is one `create_commit` with `parent_commit` set to the revision the page read, so
two writers (admin and CLI) fail loudly on a stale read rather than race. A group approval is one commit.

### 2.4 Subjects, actions and precedence

| Subject | Action group | Actions |
|---|---|---|
| roaster | status | `not_a_coffee_seller` (Phase 2), `restore` |
| product | visibility | `exclude`, `include` |
| variant | visibility | `exclude_variant` |
| batch | which fetch counts | `accept`, `reject`, `use_batch` (today's overrides) |

**One effective decision per subject and action group.** Across all decision ids, the latest `approved` or
`reverted` event for that (subject, group) wins, in curation commit order. If it is `reverted`, the subject goes
back to the pipeline's output: an older approved decision does not come back.

**Precedence across kinds: roaster > product > variant.**
- A `not_a_coffee_seller` roaster hides all its products, an `include`d one too; the admin shows that include as
  shadowed.
- A product `exclude` hides its variants; an `exclude_variant` on it is moot.
- A product `include` publishes it whatever the classifier says, and its `exclude_variant`s still apply.
- Batch decisions only choose which fetch counts; the visibility decisions then apply to its rows.

**Tested in dbt.** `int_effective_decisions` (one row per subject × action group) has a `unique` test on
(`subject_kind`, `subject_id`, `action_group`), and a unit test for each conflict: exclude then include under two ids,
an include under a not-a-coffee-seller roaster, an exclude_variant on an excluded product.

**Orphans.** `coffee_id` is `roaster_id:product_id`, so a store that moves platform gets new ids, and an excluded
coffee would come back under its new id. `decision_orphaned` (warn) lists effective decisions whose subject is
missing from the roaster's last two complete fetches while the roaster still lists products. The queue shows them
as a "re-decide" item.

### 2.5 States

```
             approved (owner; removals only when ready, 3.2)       first served build that records it
 flagged ───────────────────────────────────────────▶ approved ─────────────────────────────────▶ applied
   │   │ rejected (reason)                                │                                         │
   │   └─▶ rejected ── re-flagged only on a new evidence_hash └──────────── reverted (reason) ◀────────┘
   └─▶ expired (no build raises it any more)
```

`flagged`, `expired` and `applied` are derived. `applied` is the first served release whose build record lists
the decision. Every state has a way out, and nothing is undone by deleting.

### 2.6 What it replaces or extends

| Today | With decisions |
|---|---|
| `run_overrides` | Kept and still read (immutable). `stg_run_overrides` reads overrides plus effective batch decisions. `coffeedata override` writes a decision from then on |
| `config/roasters.csv` | Stays the curated seed in git: a list of candidates, not a verdict |
| Opt-out (issue form) | Stays, and needs no second signal: it is the roaster's own request |
| `NOT_A_ROASTER_SITE` | Stays in code: a tested rule about platforms. One business that isn't a roaster is a decision instead |
| Crawl method, a blocked store (#110) | Configuration, so in git (a `config/` file read by `select_states`), not decisions. Coverage flags still show in the queue, as blockers |

### 2.7 Who owns which contract

The owner owns every contract (the public contract's `team` block). Changed where:

| Contract | Changed by | What a decision does |
|---|---|---|
| Coffee definition + rules | PR + evaluation; a definition edit re-verdicts every version | Nothing. Many similar decisions are the evidence for a PR |
| Public catalog (ODCS 2.3.0) | the contract first, then dbt (`tests/test_contracts.py`) | No column change. Once, a minor version adds the quality rules `decisions_applied` and `classifier_guard`. After that, decisions change rows, not the contract |
| Internal raw / manifest / enrichment | `raw_contracts.py`, `make contracts` | Nothing |
| **New: internal `curation` 1.0.0** | generated from `lake.SCHEMAS` like the others | Covers `decisions` and `flags`. Adding `registry`, `verdicts` and `run_overrides` is worth doing, as its own PR |

## 3. Flags, readiness and the guard

### 3.1 Phase 1 rules, each with a direction

| Rule | Polarity | Raised when | Evidence signature |
|---|---|---|---|
| `verdict_flip` | `supports` exclude when the verdict *added* a public coffee the rules call not coffee; `restore` when the verdict *removed* one the rules call coffee | the rules and the stored verdict disagree on a product that is, or was, public | roaster, rules' answer, verdict label |
| `shared_body` | `doubts` | the product's body_hash is shared by ≥ `shared_body_min` (3) products of the roaster with different titles: the text says nothing about this product | roaster, body_hash |
| `bulk_green_weight` | `supports` exclude, or exclude_variant when the product also has retail sizes | a variant of ≥ `bulk_min_grams` (20,000) | roaster, weight class |

Later rules, each added only with a score (3.4): `roaster_churn` (doubts), `no_coffee_complete_runs` (supports
`not_a_coffee_seller`), `unsure_public`, the price outliers of #111, and the coverage signals (doubts, 3.5).
Thresholds are dbt vars, tuned by measurement.

### 3.2 Readiness

- **A removal** (`exclude`, `exclude_variant`, `not_a_coffee_seller`, or rejecting a batch whose rows are public)
  is **ready** when all three hold:
  1. at least one open `supports` flag proposes it;
  2. a `checked` event with finding `confirms` is on the decision, or covers its group (3.3);
  3. no open `doubts` or coverage flag is on the subject or its roaster.

  Then the owner approves. The admin only enables the button.
- **This is the whole rule in Phase 1:** one flag plus a live check. No rule is scored yet, so no rule counts as
  evidence on its own.
- **Later**, once two rules from different sources have held-out scores, "a scored `supports` from each of two
  sources" can replace the live check. That clause is added with the second score, not before.
- **A restore** (`include`, `restore`, `revert`) needs one `restore` flag or a reason, and the owner's approval.
- A `doubts` flag never makes anything ready. It holds.

### 3.3 Group checks

The 22 sacks shouldn't need 22 live checks. A **group** is the open flags with one rule and one evidence signature
(one roaster, one weight class). One `checked: confirms` on a named sample product, recorded with `covers =` the
group's signature, satisfies condition 2 for every member whose `evidence_hash` matches. Each member still gets its
own `approved` event (one commit), so the history stays per product.

### 3.4 The classifier guard: it holds from Phase 1

"Nothing is removed on a single signal", applied to the classifier itself. In `int_catalog_decided`:
- **When.** A coffee is in the served release, the new build's classifier says not coffee, and its removal rests on
  one signal (`verdict_flip`, the verdict against the rules) whose evidence is doubted (`shared_body`).
- **What "stays published" means.** `dim_coffee` uses that product's **last version that was coffee** instead of its
  latest version, with `held_by = 'classifier_guard'`. Prices and stock still come from the latest fetch.
- **The flag.** It raises a `restore` flag (to make it permanent with an `include`) beside the `doubts`. The owner
  can instead approve an `exclude`, which still needs a live check.
- **Bounds.**
  - The build fails `classifier_guard` (error) if the guard would hold more than `guard_max_held` (200) products.
    A deliberate wave, like the 11-04 backfill, then needs `--accept-classifier-change`, the way
    `--accept-volume-change` works now. So it's a conscious step, never a frozen catalog.
  - A hold lapses after `guard_max_weeks` (4). The coffee then follows the classifier and the flag stays.
- **Why hold rather than warn.** Holding is the abstain direction CLAUDE.md prefers when rules can't tell. A
  wrong hold keeps one non-coffee for a few weeks; a wrong removal loses a real coffee silently.
- **It is a rule.** It changes how the catalog decides, so it ships only after CLAUDE.md's full process:
  - a roaster-grouped salted split, two labelers and a third;
  - "was the held product coffee?" with Wilson intervals;
  - the sealed test scored once against `main`.

  The measurement tunes its thresholds; it isn't what switches it on.
- **Decisions are labels.** They correct one item each. `products.decide` taking a decision changes output only on
  decided products, and those are counted toward step 9, each checked by hand. Approved decisions go to the
  **train** set and the regression fixtures, never to a sealed test set. The eval scores the classifier before
  decisions.

### 3.5 Coverage: fix, don't remove

A roaster we read badly looks like one with nothing to sell. A coverage signal is a `doubts` on every removal for
that roaster, and its fix is in git:
- **Squarespace:** the adapter stops at the first collection with items (`if seen: break`,
  `adapters/squarespace.py`). The fix is a code change for every Squarespace store, measured on `eval/crawl`.
- **A redirect loop for our bot** (`TooManyRedirects`) and **a 403 on the product feed:** the fix is a crawl-method
  line in `config/`, and for the feed, the secondary crawler (#67) after its held-out score.
- **Challenges:** a 202 or 429 stays an ordinary failed fetch, retried every pass and every week. The store is
  marked blocked only after 6 consecutive weeks of the same challenge (#110, January 2027), in config.

### 3.6 The worked examples

| Case (this week) | Flags | Outcome |
|---|---|---|
| 16 Stivers single-origins removed (the same pasted "Sample Pack" text) | `verdict_flip` restore + `shared_body` doubts | **Held** by the guard from the next build. The owner group-approves 16 `include`s with one reason. The body_hash becomes a regression fixture |
| 3 drink mixes added | `verdict_flip` supports exclude | A live check on each, then `exclude` ×3; a unit test each |
| 5 businesses that sell no coffee (roaster parts, nut roastery, bakery, bookstore, a caffeine-free substitute) | Phase 1: product-level `exclude`s after a live check. Phase 2: `no_coffee_complete_runs` | Phase 2: `not_a_coffee_seller` after a live check; out of the weekly crawl |
| 6 real roasters we fail to crawl | coverage doubts; no removal can be ready | Fixed in git (3.5) |
| 22 green-coffee sacks public (e.g. copantrade.com "BRAZIL Coopama MTGB Sc 14/16 - 60 KG BAGS") | `bulk_green_weight` supports | One group check on a sample, then 22 `exclude`s |
| Unsure verdicts (4,177 version rows) | none in Phase 1 | They go to labeling, not the queue: one person can't read 4,000 |

## 4. How dbt applies decisions

### 4.1 Models

Every new model is documented in `dbt/models/schema.yml`, starting with its grain:
- **Sources.** `curation.decisions` is a `source()`, and so is `lake.flags`. `flags` has a freshness check (warn
  when the newest build's flags are older than 8 days); decisions can go quiet for weeks, so they have none.
- **`stg_decisions`**: one row per decision event.
- **`int_effective_decisions`**: one row per subject × action group (2.4), with the `unique` test.
- **`int_catalog_decided`**: one row per candidate product. It holds the classifier's answer, the effective
  decision and precedence, the guard, and the outcome. Excluded rows stay, with their `decision_id`; it is exported
  to the private `marts/` for the admin.
- **`dim_coffee`** reads `int_catalog_decided` and keeps its meaning (published coffees, plus `held_by` and
  `decision_id`), so the app, MCP and site need no new filter.
- **The one coffee decision stays one.** `products.decide(item, label, decision)` takes the effective product
  decision, so enrichment (an excluded product is never sent to Claude) and the registry agree with the build.
- **Public tables filter, and their columns don't change.** No decision ids reach them.

### 4.2 Tests

- **Unit tests**, in dict format:
  - exclude, include over a verdict, revert, exclude_variant;
  - the three conflicts in 2.4;
  - the guard: held, lapsed after `guard_max_weeks`, over `guard_max_held`.
- **Singular tests, named one by one in the gate output:**
  - `decisions_applied` (error): every effective exclude is absent from the public tables, and every include is
    present when its product exists. If it fails, find why; never downgrade it to get a build through;
  - `classifier_guard` (error over the cap);
  - `flags_have_evidence` and `decision_orphaned` (warn);
  - relationships between flags and decisions (warn).
- **Development:** `dbt build --select stg_decisions+`.

### 4.3 Release notes

#111's `release_changes` gives every changed public row a reason. Decisions add `decision:<action>` (with the id),
and the guard adds `held:classifier_guard`. `coffeedata build` prints the counts by reason, and `builds` keeps
them. Counts on the site and in `manifest.json` are deferred.

### 4.4 Reproducible

`coffeedata build` reads the curation dataset at one commit, and records that commit sha plus every decision id it
applied in `builds` (`kind = 'decision'`). A rebuild of that release reads the curation dataset at the recorded
sha (`hf://datasets/…@<sha>`, the revision form `Lake` already parses). It never reads by a cutoff time, since
`event_at` is a client's clock. `rollback` re-serves a release, so it is unaffected. A decision takes effect at the
next build, never on a served release.

## 5. admin.registry.coffee

### 5.1 Pages and flows

| Page | Phase | What it shows | Actions |
|---|---|---|---|
| **Queue** | 1 | Open items grouped by subject, then roaster and rule; sorted by `public_rows`, then weeks open. One summary per row ("1 supports · 1 doubts · held"), its state, and "ready" or "needs a live check" | open a case or a group |
| **Case** | 1 | The flags with their polarity and evidence: rules' answer, verdict (model, definition), title, type, tags, sizes and prices, the description, how many products share it, the roaster's other products, 8 weeks of fetch statuses. Its state, and once applied, the release | record a live check; approve or reject with a reason. A disabled approve says why. A removal's confirm names how many public rows leave |
| **Group** | 2 | One rule × one evidence signature (16 Stivers, 22 sacks) | one live check on a sample; approve or reject all with one reason |
| **History** | 2 | Every decision, by subject, actor or action, with the release that applied it | revert, with a reason |

Deferred: a server-side live fetch, the Contracts page, and a Coverage page (coverage fixes live in git).

**Every state is defined.**
- **Empty:** "Nothing to review", with the build the queue is as of.
- **Loading:** the start-up reads are tens of MB, so show "Reading build X…".
- **Stale:** when a newer build exists than the one loaded, a banner says "As of build X" with a reload.
- **Failed write:** keeps the typed reason, shows the error, offers a retry on the same path (idempotent), and says
  plainly that nothing was recorded.

### 5.2 Reads and writes

- **Reads, from the lake with a read-only token:** the private `marts/` (with `int_catalog_decided`), the latest
  `flags`, and the bodies of the products in the queue only.
- **Reads and writes, in the curation dataset:** `decisions/`. Writes are idempotent paths, `parent_commit`, and one
  commit per action or group (2.3).
- **No database.** The build must read decisions to be reproducible, and it reads Hugging Face. A second store would
  need a sync, a backup and a second audit trail, for a few hundred rows a year.

### 5.3 Untrusted text

Titles, tags and descriptions are third-party text, shown on the one page that holds a write token.
- They render as escaped text (Jinja autoescape, never `|safe`).
- The admin keeps `app/web.py`'s Content-Security-Policy and its `Referrer-Policy: same-origin`, so store links
  don't send admin URLs out.
- Only `https:` store links are rendered as links.
- Writes keep the app's CSRF rule (`X-Requested-With`, plus `Sec-Fetch-Site` / `Origin`).

### 5.4 Hosting: its own Cloud Run service, from the app's image

A separate service, not routes in app.registry.coffee.
- **Access:** the app's service has an `allUsers` invoker; this one has none, and IAP sits in front.
- **Blast radius:** an admin bug or memory spike can't take down the shot logger (one instance, 1 GiB, its state
  in memory).
- **Secrets:** its own service account and a token with lake read and curation write. No Anthropic key at all.
- **Terraform is additive** (from a reviewed plan only):
  - `google_cloud_run_v2_service.admin` (min 0, max 1, `iap_enabled`);
  - `google_service_account.admin`;
  - `roles/iap.httpsResourceAccessor` for the owner's Google account;
  - the secret `ADMIN_HF_TOKEN`;
  - a domain mapping (`admin` CNAME → `ghs.googlehosted.com`);
  - a `terraform test` plan assertion: no `allUsers` member, and `iap_enabled` true.
- **To verify before that PR:**
  - `iap_enabled` exists in `hashicorp/google ~> 6.0`, the pin in `provider.tf`. If it is only in `google-beta`,
    that is a provider upgrade risk to plan separately;
  - IAP works with a domain mapping here. If not, use the `run.app` URL. A load balancer (about $18 a month) costs
    more than everything else together;
  - **the deploy gap.** `deploy.yml` runs `gcloud run deploy "$SERVICE"` for one service, so a shared image would
    leave the admin on an old schema. Deploy both services from the same image in one job.

### 5.5 Auth: IAP, and a check in the code that fails closed

The repo is public, so the routes are known; obscurity protects nothing.
- **IAP** puts Google sign-in, with 2-step verification, in front.
- **In the code**, every request needs a valid `X-Goog-IAP-JWT-Assertion`: its signature, the `aud` pinned in
  config, and the email equal to `ADMIN_EMAIL`. Anything else is a 403, and a test covers that.
- **Why the check matters:** the deploy service account holds `roles/run.developer` (`deploy.tf`), which can change a
  service's settings, IAP included. The in-code check is what holds if IAP is turned off.
- **Locally**, `make admin` uses a stub like `auth.LocalOwner` that refuses to start when `hosted()`.
- **The app's sign-in alone isn't enough:** a password with no second factor and 90-day sessions is too little for
  the thing that edits the public dataset.

### 5.6 Audit, alerts, cost

- **Audit.** Three records that agree: decision files (who, when, why, the frozen evidence), the curation dataset's
  commits, and `builds` plus `serving_log` (which release applied each one). Cloud Run's request logs add the IAP
  identity.
- **Logs.** One JSON line per write: `decision_id`, `action`, `subject_kind`, the outcome. No text, no tokens.
- **Alerts.**
  - A failed curation write logs at ERROR, which the existing error policy emails.
  - A check after each build warns on an approved decision that no served build has recorded within 8 days (one
    weekly cycle). `decisions_applied` catches the reverse case.
- **Cost.** Scale to zero, inside the free tier; IAP, the domain mapping and the dataset are free. About **$0 a
  month** on top of today's $9–14. The admin calls no model API.

### 5.7 The UI: the app's design, not a new one

- **Bench Scale.** `app/static/app.css` (the tokens, Archivo and Newsreader, the scale in `docs/app/DESIGN.md`),
  the same light and dark themes, and the existing components: list link, chips, status text, spec sheet and
  notice. The header matches the site's nav. No new colours: `--danger` only on a removal's confirm.
- **A familiar review pattern:** list, detail, decide, history. Every state has a text label as well as a colour.
  No dashboard tiles, no motion.
- **Keyboard:** `j`/`k` and `a`/`r` fire only while the queue list has focus, and a setting turns them off
  (WCAG 2.1.4). Focus is visible, controls are real buttons with labels, and text keeps the 4.5:1 contrast the
  tokens already meet.

## 6. Alternatives

| Option | Evidence | Decide | Audit | Cost | Verdict |
|---|---|---|---|---|---|
| **Admin app (recommended)** | Yes, private text included | Yes, readiness enforced | Curation commits + builds | ≈ $0 | Build it, after the data model, locally first |
| Issue forms (this repo) | The repo is **public**: the evidence and labels like "not a coffee seller" would be public | A label or comment | Excellent | $0 | Keep for adding and opting out roasters. Not the queue |
| A private curation repo, decisions as PR'd CSV | Poor: no descriptions or fetch history beside the item | A merged PR | Excellent, with 2FA | $0 | The one real alternative. Clumsy for 16 or 22 items, and it still needs a viewer for the evidence |
| Grafana / Metabase | Good for reading | No write-back | Weak | Free tier, or a VM | Later, perhaps, for crawl health (#93). Not for decisions |
| Retool / Appsmith | Quick tables and buttons | Through an API we'd still write | The tool's own | Free tier, or a VM | The HF token and the private text would sit with a third party. Not worth it |
| CLI only | Weak for reading evidence | Yes | The same | $0 | Built anyway: the second writer, and the break-glass path (section 8) |

The owner's choice is right for items. It would go wrong if the hosted app came before the model (then the app is
the model and the build can't reproduce it), or if the admin edited definitions, rules or contracts. That would
skip the evaluation and, for the definition, re-verdict every product.

## 7. Phasing

**Phase 1: the smallest useful step.**
- The curation dataset; `decisions`; and `coffeedata decide` (check, approve, reject, revert), with
  `coffeedata override` writing decisions too.
- dbt applying `exclude`, `include` and `exclude_variant`, with the precedence, the uniqueness test and the
  conflict unit tests; `decisions_applied`.
- The classifier guard, holding (after its full evaluation, 3.4).
- Three flag rules: `verdict_flip`, `shared_body`, `bulk_green_weight`.
- Queue and Case on localhost (`make admin`).
- The curation contract, 1.0.0.
- This week's cases decided through it.

**Phase 2:**
- Group (with group checks) and History;
- `not_a_coffee_seller`, with `registry.STATE_SQL` and `dim_roaster` agreeing;
- hosting: the service, IAP, the JWT check, the deploy fix, the alerts;
- the three runbook entries (section 8), in `runbook/troubleshooting.md`.

**Phase 3:**
- more rules, each scored, and the two-source readiness clause once two have scores;
- release-note counts in public;
- the curation contract extended to `registry`, `verdicts` and `run_overrides`.

**Deferred until needed:**
- decision expiry dates and `subject_changed` re-flags;
- a server-side live fetch, and a Contracts page;
- attribute corrections, and merging near-duplicate roasters (#111 rule 12).

## 8. Risks and runbook entries

| Risk | Mitigation |
|---|---|
| The guard holds real non-coffees, or freezes a roaster | Bounded by `guard_max_held` and `guard_max_weeks`; measured before it ships; every hold is in the queue |
| The queue outgrows one person | Sort by public impact; group checks; unsure verdicts go to labeling; flags expire when their evidence goes |
| Unscored flags are trusted | No rule makes a removal ready alone; Phase 1 needs a live check |
| An XSS through roaster text | Escaped rendering, the CSP, `https:` links only (5.3) |
| A token that can rewrite the lake | The admin's token can't: it has lake read only (2.1) |

Each runbook entry covers detection, triage, mitigation, verification and rollback:

- **A bad bulk decision went out.**
  - *Detect:* the volume check, `release_changes` counts by `decision:` reason, or the owner noticing.
  - *Mitigate:* `coffeedata rollback` serves the previous release at once.
  - *Resolve:* one `revert` per decision (a group revert is one commit), then rebuild.
  - *Verify:* `decisions_applied` passes, and the counts by reason are back to the earlier release's.
- **Admin writes fail.**
  - *Detect:* the ERROR alert, or the page's failed-write state.
  - *Triage:* a `parent_commit` conflict (reload, then retry) or an HF outage or token problem.
  - *Mitigate:* retry; the path is the same, so a retry never duplicates an event.
  - *Verify:* the event file is in the curation dataset.
- **Locked out of IAP** (a lost account, a broken binding).
  - *Break glass:* on the laptop, `coffeedata decide …` with the curation token writes the same events; the build
    applies them as usual.
  - *Then:* fix the IAM binding from a reviewed Terraform plan.

## 9. Open questions for the owner

1. **The guard's scope.** It holds the classifier's removals of public coffees only when the one signal behind the
   removal is doubted (a shared description), capped at 200 products and 4 weeks. Is that the reading of "nothing is
   removed on a single signal" you meant? Or should it hold every verdict-only removal, which would have held most
   of the backfill's 3,765?
2. **Restores.** Should putting a coffee back (`include`) need your approval? Proposed: yes, with one signal
   enough.
3. **"Not a coffee seller"** (Phase 2): stop crawling it until a re-check, or keep crawling it monthly?
4. **Release notes in public** (Phase 3): counts by reason only (proposed), or the named roasters and coffees too?
5. **Sign-in:** which Google account, and is the `run.app` URL acceptable if IAP can't sit behind the custom
   domain?
6. **Green coffee by the sack:** handle it with decisions and the weight rule (proposed), or add it to the
   definition and pay for a full re-verdict?
7. **Group checks:** is one live check on a sample enough to cover a group with the same evidence (proposed), or do
   you want a check on each item?

## Skills used

- **dbt** (2.4, section 4):
  - dbt Labs `using-dbt-for-analytics-engineering` (extend the existing layers, declare the grain, look at the
    data first; the counts above come from a local read-only lake copy);
  - `adding-dbt-unit-test` (dict fixtures for the conflict and guard cases);
  - `maintaining-dbt-documentation` (the house style in `schema.yml`, grain first);
  - `troubleshooting-dbt-job-errors` (never weaken `decisions_applied` to pass);
  - wshobson `dbt-transformation-patterns` (staging, then intermediate, then marts).
- **Data quality** (section 3):
  - wshobson `data-quality-frameworks`: a versioned contract, thresholds as variables, and warn before block,
    except the guard, which is the safe direction.
- **DevOps** (sections 5 and 8):
  - `terraform-skill` (blast radius, secret exposure, provider upgrade risk, a reviewed plan);
  - HashiCorp `terraform-test` (the plan assertion on `allUsers` and `iap_enabled`);
  - wshobson `python-resilience` (bounded, idempotent retries);
  - `python-observability` (structured logs, bounded fields, an alert);
  - `deployment-pipeline-design` (deploy both services, gates, rollback);
  - `incident-runbook-templates` (the three entries).
- **Design** (5.1, 5.7):
  - MengTo `no-ai-design-slop` (keep the local system, hierarchy before labels, no dashboard filler);
  - Owl-Listener `jakobs-law`, `state-machine` and `accessibility-audit`.
