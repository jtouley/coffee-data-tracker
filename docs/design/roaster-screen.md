# Screening discovery candidates for roasters that sell beans online

Status: proposal, 2026-11-16. Nothing here is built. Owner: jtouley. Checked against `origin/main` at `36d5e3d` and
draft PR #131 (`admin-sources`, `70712ae`). No roaster, café or importer site was fetched for this note.

**What the owner asked for.** Screen the discovery candidates by probing politely, state by state, over weeks; for a
supported platform read only the first page of products; run the existing product classifier; score the place as a
roaster from its catalog; and route by confidence: high to "Suggested roaster" on the Sources page for bulk
acceptance, unsure to review, low dropped. Abstain rather than guess.

**The design in seven lines.**
1. A new `screen` job reuses the probe (`discover.probe_one`) with a larger page size, so a Shopify or WooCommerce
   candidate costs the probe's own requests and nothing more: about 3 to 4 requests per place, once.
2. The job only loads raw rows (`screens`, `screen_products`), append-only. Every judgement is derived in dbt.
3. dbt computes features per screen from the existing classifier and extractors (`products.decide`,
   `extract.rules`, `classify.is_wholesale`, bag weights), then a rules-first tier: suggested, review, dropped, or
   not screened.
4. Claude sees only the review tier, with a written definition, and can promote a place to suggested. It never drops
   one.
5. The served release's `screen_scores` ops table feeds a "Suggested roaster" status on Sources (#131). Bulk accept
   writes ordinary `roaster` flags, with the screen's evidence attached, and they reach the crawl only through a
   published draft, as today.
6. It is measured under CLAUDE.md in full. #130's labels and split are reused, with a fresh sealed test, the 210
   curated roasters as positives, and floors.
7. Rollout is seven small PRs. Nothing is shown to the owner until the test score is in.

## 0. Premise and existing work

| Item | What it says | What this design does with it |
|---|---|---|
| `notes/missing-roasters/germackcoffee.md` (branch `ccr-53fdc95c-2qdy7y`) | The seed's Overture filter (`discover.overture()`, `src/coffeedata/discover.py:124-185`) keeps a place only if it is tagged `coffee_roastery` or has "roast" in its name. It found 96 of the 210 curated roasters (45.7%, Wilson 39.1–52.5%). About 3,700 US roasters are missing (range 2,800–4,800) | The gap this closes, for candidates that are in Overture with a website |
| #130 (closed, `discover-cafes`, `3c8af60`) | A screen that **admitted** cafés to the registry. 8-page budget, rule `own_brand` with N = 5. Sealed test: precision 89.4% [62.1, 96.3], recall 37.5% [28.4, 62.5], 2 of 15 admissions a café or reseller (`eval/screen/RESULTS.md`). Closed by the owner on 2026-11-07: "crawl roasters only … No screen gate" | Reused: the labelling guide, the split rule and salt, 400 train and validation labels, and the reseller and brand checks (`screen.py:86-175`). Changed: first page only; raw rows only (#130 wrote derived decisions into the lake); and the screen **suggests**, the owner decides |
| #131 (draft, `admin-sources`) | The `candidates` lake table (`src/coffeedata/lake.py:222` on that branch); `control.source_flags`, append-only (`admin/migrations/0004_sources.sql`); flags reach the registry through the draft snapshot (`registry.current`, `registry.py:336`); Pull now (`admin/jobs.py:128`) | The place list it screens, and the only path a screen result takes to the crawl |
| #135 (`admin-prefilter`) | A preview before a save, and a save token bound to that preview | The bulk-accept preview copies it |
| #149 (`admin-chains`), `eval/chains/README.md` | A chain is more than 20 stores; it is crawled but left out of public tables | A chain can still be suggested. The chain rule decides what is public |
| Issue #70 | Canada and Mexico | Out of scope. The screen is US only, like `candidates` |

**This reverses one 2026-11-07 decision.** `notes/NEXT-STEPS.md:307` says "No site is fetched to decide what to
crawl." The direction now agreed fetches one product page per candidate to make a *suggestion*. The flag stays the
owner's, so it is still not a gate. Decision D1 asks the owner to confirm this in writing.

## 1. Goal and non-goals

**Goal.** Among the roughly 44,000 `candidates` not already in the registry (#130 counted 43,655 café and
coffee-shop domains on Overture `2026-10-21.0`; #131 lists about 47,000 places including roasteries), find the ones
that are **roasters selling their own coffee by the bag online** (section 5.1). Hand them to the owner as a short,
high-precision list to accept in bulk, plus a smaller list to review.

**Non-goals.**
- No automatic admission. A place enters the registry only through the owner's `roaster` flag and Publish.
- Cafés and resellers stay out (owner, 2026-11-07). Finding which roaster a café serves is not part of this.
- Places missing from `candidates` (no Overture listing, no website, a social-media link) are out of reach. OSM and
  the add-roaster form cover them.
- Unsupported platforms (Wix, Webflow, custom builds) get no product read. They are scored from the home page alone,
  or not screened.
- No change to the weekly probe, crawl, classifier, or public tables.

## 2. The pipeline, stage by stage

```
candidates (lake, #131) ─▶ select: one state, not in registry, not flagged, due ─▶ screen job (Wed, Thu)
   per place: robots.txt → home page → first product page (Shopify / WooCommerce)      [PoliteClient]
   ─▶ lake: screens/, screen_products/  (append-only, private)
   ─▶ screen-verdicts: review tier only, Message Batches  ─▶ lake: screen_verdicts/
   ─▶ dbt (Tuesday build): stg_screen* → int_screen_products.py → int_screen_features → screen_scores (ops)
   ─▶ admin Sources: "Suggested roaster" / "Needs review" ─▶ bulk accept = source_flags 'roaster' ─▶ draft → Publish
   ─▶ registry.current ─▶ probe ─▶ weekly crawl   (unchanged)
```

### 2.1 A new job, not a mode of probe

The job is new: a `screen` Cloud Run job and a `coffeedata screen` command. It reuses the probe's code, not its
schedule.
- **Why its own job.** The probe has a 100-minute window before the Monday crawls (`src/terraform/ingestion.tf:114`,
  `:171`). The screen must never eat into that window or delay the crawl. The screen also has its own population, its
  own re-screen windows, and its own `job_manifests` name (`control.start(lake, "screen", …)`, as `cli.py:230` does
  for the probe).
- **Why the same code.** `discover.probe_one` (`discover.py:282-332`) already asks robots.txt, fetches the home page,
  and asks `/products.json?limit=1` and the WooCommerce Store API with `per_page=1` (`discover.py:316-329`). The screen
  asks the same URLs with the crawl's page size: Shopify `limit=250` (`adapters/shopify.py:35`, `PAGE_LIMIT`) and
  WooCommerce `per_page=100` (`adapters/woocommerce.py:30`). That page is the screen's evidence, so a Shopify
  candidate costs exactly what a probe does.
- **The one change to shared code.** `probe_one` becomes a thin wrapper over `_probe(client, domain, page_size=1)`,
  which also returns the home response and the parsed JSON. With the default, the probe's rows are byte-identical,
  checked by the existing `tests/test_discover_probe_jobs.py` plus one new parity test.

### 2.2 Where each piece lives

| Piece | Where | Reuses |
|---|---|---|
| Selection, run loop, per-place screen | `src/coffeedata/screen.py` (new) | `discover.probe`'s loop with `on_batch` writes, deadline and flush (`discover.py:342-409`); `cli.probe_todo`'s ordering (`cli.py:210`) |
| Product parsing | unchanged adapters | `shopify.parse_product` (`adapters/shopify.py:139`), `woocommerce.parse_product` (`adapters/woocommerce.py:91`). No variation-price lookups (`woocommerce.variation_prices`, `:256`); prices aren't needed |
| Phase 2 platforms | `screen.py` | `squarespace.COMMON_SHOP_PATHS` and `_collection` (`adapters/squarespace.py:18`, `:73`); `square_online.site_ids` from the home page already fetched (`adapters/square_online.py:43`) |
| Features to tier (pure functions) | `src/coffeedata/screen_rules.py` (new) | `products.decide` (`products.py:41`), `classify.is_wholesale` (`classify.py:125`), `extract.rules` (`extract.py:329`), `bag_weight.product_grams` (as `dbt/models/staging/stg_variant_grams.py:19` calls it); #130's `brand_key`, `is_own`, `KnownRoasters`, `ROAST_WORDS`, `SERVES_WORDS` with its review fixes (`3c8af60:src/coffeedata/screen.py:86-175`) |
| Claude on the undecided | `src/coffeedata/screen_verdict.py` + `screen_definition.md` (new) | `verdict.py`'s shape: `pending` (`:170`), `submit` (`:192`), `collect` (`:250`), a definition versioned by its hash (`:36-38`), work derived rather than tracked |
| Raw tables | `src/coffeedata/lake.py` `SCHEMAS` | `write_partition` (`lake.py:887`) |
| dbt | `dbt/models/staging/stg_screens.sql`, `stg_screen_products.sql`, `stg_screen_verdicts.sql`; `intermediate/int_screen_products.py`, `int_screen_features.sql`; `marts/screen_scores.py` (ops, not public) | `stg_products.py`'s pattern: a dbt Python model calling `coffeedata` code |
| Admin | `admin/sources.py`, `admin/controlplane.py`, a new migration, `part_sources.html` (the #131 stack) | `SourceList._make` (`admin/sources.py:120`), `flag_sources` (`admin/controlplane.py:443`), undo (`:501`) |
| Job and schedule | `src/terraform/ingestion.tf` `local.ingest_jobs` and schedules (the owner applies) | the `probe` entry's shape (`ingestion.tf:114-117`) |
| Evaluation | `eval/roaster_screen/` (new); `tests/test_roaster_screen_accuracy.py` (new) | #130's `eval/screen/`; `tests/evalfloor.floors` (`tests/evalfloor.py:101`); `eval/sealed/score_sealed.py`'s print-totals-only scoring |

### 2.3 Selection

A place is due when all of these hold:
- it is in `candidates` and in the state being screened;
- it is not in the registry (`registry.current`);
- it has no owner flag (Roaster, Not a roaster, or a chain flag);
- it is not opted out (`registry.py:107`);
- it passes `registry.is_storefront_host` (`registry.py:89`);
- its last screen is older than its re-screen window (section 3.5).

Never-screened places go first. After that, the order is a salted hash of the domain, so a run cut off by its
deadline carries on next time. The state comes from `--state`, or the job's own wave list (D5). Each place is
screened once per run.

### 2.4 Raw tables (append-only, immutable, in the private lake)

Every row is written once under `run_date=…/batch=<id>`. No partition is rewritten, and a re-run adds rows. A
re-screen is a new row, never an update. Nothing derived is stored: no tier, no score, no coffee count.

| Table | One row per | Columns |
|---|---|---|
| `screens` | screen of one place | `run_date`, `batch_id`, `screen_id` (uuid), `screened_at`, `roaster_id` (domain), `state`, `status` (the probe's vocabulary: `ok`, `blocked`, `unreachable`, `robots_unavailable`, `http_<code>`), `refusal` (`robots_disallow`, `robots_401_403`, `challenge`, `http_429`, null), `http_status`, `final_host`, `platform`, `page_size`, `products_read`, `page_full` (the page was full, so there may be more), `home_title`, `home_text` (visible text, first 10,000 characters), `requests`, `bytes`, `crawl_delay_s`, `duration_s`, `code_version` |
| `screen_products` | product read in a screen | `screen_id`, `roaster_id`, `product_id`, `handle`, `title`, `vendor`, `product_type`, `tags`, `variants` (`title`, `grams`, `price`, `currency`, `available`), `body_hash`, `body_text` (first 1,500 characters, as `stg_products` reads) |
| `screen_verdict_batches`, `screen_verdicts` | Claude batch / answer | as `llm_batches` and `verdicts`: `evidence_hash`, `model`, `definition`, `label` (`roaster_online`, `not`, `unsure`), `reason` (one sentence), `usage` |

These are private: they hold third-party listings of places that aren't in the registry. They are never exported,
and `raw_contracts.py` and `contract_docs.py` mark them internal.

### 2.5 dbt derivation (idempotent)

- `int_screen_products.py`: for each product, `products.decide(item, None)` (rules only), `classify.is_wholesale`,
  `extract.rules(item)` (roast level, origin country, blend), the bag grams, and #130's `named_coffee` (coffee named
  in the title, type or tags).
- `int_screen_features.sql`: one row per `screen_id` with the features in section 4.1.
- `screen_scores.py`: one row per domain. It takes the latest screen (`arg_max` over `(screened_at, batch_id)`), the
  tier from `screen_rules.tier(features)`, and the Claude verdict for that screen's `evidence_hash` under
  `var('screen_verdict_model')` and the definition version. Its columns:
  - `tier`
  - `decided_by` (`rules` or `claude`)
  - `reasons` (signal codes such as `own_vendor 8/9`, `origins 4`, `bags 340g,907g`, `serves:Stumptown`)
  - `rule_version`
  - `screened_at`
  - `next_due`, computed from `screened_at`, never from the clock
- **Idempotent.** The output is a pure function of the raw tables, the seeds, the vars and the code. There is no
  `now()`, the tie-breaks are deterministic, and nothing depends on an earlier build's tables. A rules change re-scores
  every past screen without a request.
- **Tests:**
  - `unique` and `not_null` on `screen_scores.roaster_id`;
  - `accepted_values` on `tier`;
  - a test that a `suggested` row has no reseller code in `reasons`;
  - dict-format unit tests for the latest-screen tie-break and the verdict join.
- **The pending set and dbt agree.** `screen_scores` is an ops table written beside each release, as #129's `mart_flags`
  is. The Claude step's pending set uses the same `screen_rules.tier`, so the two can't disagree.

## 3. Request budget and politeness

### 3.1 Per candidate

| Path | Requests | Notes |
|---|---|---|
| Shopify | 3: robots.txt, `/`, `/products.json?limit=250` | +1 robots.txt and +1 hop when `/` redirects to `www.` |
| WooCommerce | 4: robots.txt, `/`, `/products.json` (refused or HTML), `/wp-json/wc/store/v1/products?per_page=100` | The order follows the home page's markers (`HTML_MARKERS`, `discover.py:271`): with a `woocommerce` marker, the Store API is asked first and the Shopify request is skipped (3) |
| Other platforms, phase 1 | 2–4 | The API guesses are skipped when a marker names another platform. Scored from the home page only |
| Squarespace, Square Online (phase 2) | +1 | `/shop?format=json` or the Square API |
| Refused, dead or blocked | 1–2 | robots.txt only, or robots.txt and `/` |

**Hard cap: 6 requests per place, redirects and robots.txt included**, counted by `RequestStats`
(`http.py:537`, `client.track()`). A place that reaches the cap is recorded as is, with what it has read. Expect
about 3.5 on average. #130 measured about 7 with an 8-page budget.

### 3.2 In total

About 44,000 places × 3.5 ≈ **155,000 requests, spread over about 8 weeks**, each host asked a handful of times,
once. That is about 10,000 requests per run (3,000 places). The pace is a choice (D5).

### 3.3 Pacing and robots.txt

- **The client.** `PoliteClient(min_interval=1.0, timeout=12, retries=2, deadline_s=…)`, as the probe uses it
  (`discover.py:376`): at least 1 s between requests to a host, or the host's robots.txt `Crawl-delay` up to 60 s
  (`http.py:41`, `_wait_turn` `:832`).
- **The user agent.** `RegistryCoffeeBot` (`http.py:33`), with its contact page.
- **Workers.** 16, as the probe and #130 used: 63 to 101 places a minute, measured in #130.
- **Shopify's shared budget.** Requests to Shopify stores go through `client.for_platform("shopify")`, so they share
  Shopify's limiter: 3 requests a second, 8 concurrent (`ratelimit.py:35-36`). Most candidates are separate hosts on
  the same edge.
- **robots.txt is read before anything else** (`PoliteClient._allowed`, `http.py:716`):
  - a URL it disallows is not fetched;
  - a robots.txt that answers 401 or 403 disallows everything;
  - a challenge, a 429, a 5xx or no answer means nothing is fetched (`_fetch_robots`, `http.py:769`).
- **No retries beyond the client's own.** There is no second egress and no other user agent, and a challenge is
  never worked around.
- **A circuit breaker.** If more than 8% of a run's first contacts are refused (the same threshold as the crawl's
  egress check, `control.py:23`), the run stops starting places and records `stopped: refusals`.
- **Egress: Cloud Run's default** (D6), never the Shopify or WooCommerce NAT IPs (`ingestion.tf:5-9`). A screen's
  refusals must never spend the crawl's IP reputation.
- **Schedule.** Wednesday and Thursday at 14:17 UTC with a 90-minute deadline. That keeps clear of Monday's crawl
  passes, Tuesday's merge, enrich and build chain (`ingestion.tf:170-195`), and discover on the 1st. **This week's
  crawl is not touched.** Nothing runs until the owner applies the job.

### 3.4 How a refusal is recorded

Every refusal is a `screens` row: `status` and `refusal` say what the site said, and nothing else is fetched.
- `robots_disallow`: robots.txt disallows `/` or the product path. The tier is `not_screened`.
- `robots_401_403`, `challenge`: the site, or its edge, refuses us. The tier is `not_screened`. The reason is shown as
  a hint on Sources, so the owner can still flag the place by hand.
- `http_429`, `robots_unavailable`, a 5xx: transient. Retried after 28 days.
- `unreachable` (DNS or connection) and `http_404` or `http_410` on `/`: a dead site. The tier is `dropped`.

### 3.5 Re-screening

| Last result | Re-screened after |
|---|---|
| Transient (`robots_unavailable`, 429, 5xx, a run's deadline) | 28 days. Two tries, then 180 days |
| Dropped (no coffee for sale, a dead site) | 180 days (#130's window: a café becoming an online roaster is slow and rare) |
| Refused by robots.txt or a 401/403 | 365 days: the site said no |
| Suggested or review, with no owner action | 180 days (the evidence goes stale) |
| Flagged by the owner | never: the flag wins |

## 4. The roaster-level classifier

### 4.1 Features (per screen, computed in dbt)

| Feature | From | Reads as |
|---|---|---|
| `products`, `page_full` | `screens` | How much of the catalog was seen |
| `coffees`, `named` | `products.decide(item, None)`, #130 `named_coffee` | Whole-bean or ground coffee for sale. `named` guards against café stores whose jams and gift boxes the rules call coffee (#130's train finding) |
| `coffee_share` | `named / products` | A roaster's page is mostly coffee; a café's is mostly merch and gift cards |
| `bag_share`, `bag_sizes` | variant grams 150–2,500 g (`classify._bag_sized`, `classify.py:121`); distinct sizes | 12 oz, 2 lb and 5 lb options per coffee are a roaster's pattern |
| `roast_share` | `extract.rules(item).roast_level` not null | Roast levels named |
| `origins`, `origin_repeat` | distinct `origin_country`; origins on 2 or more coffees | A rotating single-origin menu |
| `own_vendor_share` | `vendor` brand key equal to the store's name or domain stem (#130 `is_own`, exact, never a substring) | Its own name on its bags |
| `other_roaster` | a vendor, or a multi-word name in a title, matching a registry roaster's name (`KnownRoasters`) | The café-reseller case |
| `vendors` | distinct vendors among coffees | 3 or more: a multi-roaster shop |
| `roast_words`, `serves_phrase` | home page text only (`ROAST_WORDS`, `SERVES_WORDS`) | "We roast in house" against "Proudly serving Stumptown". Never read from product text, which a reseller copies |
| `wholesale_share` | `classify.is_wholesale` | A wholesale-only store |
| `currency` | the variants | Not USD: out of scope (US registry) |
| `locations`, `tag` | `candidates` | Context, not a vote: a chain can be a roaster |

### 4.2 Rules first (provisional; thresholds are tuned on train and chosen on validation)

1. **not_screened.** Refused, blocked, or the robots.txt never answered; or an unsupported platform with no roastery
   wording. Abstain: no suggestion, and the reason is shown as a hint.
2. **dropped** (confidently not). A dead site; or `products ≥ 1` with `named = 0`, no roastery wording, and either
   `page_full = false` or `products ≥ 50`; or the currency isn't USD.
3. **review** (reseller doubt). Any of these:
   - `other_roaster ≥ 1`;
   - `vendors ≥ 3`;
   - a `serves_phrase` without roastery wording;
   - `wholesale_share ≥ 0.8`.

   A doubt always goes to a person. It never becomes a suggestion, from the rules or from Claude.
4. **suggested** (confidently a roaster selling online). All of these:
   - `named ≥ N` (#130 chose N = 5);
   - `coffee_share ≥ s`;
   - `bag_share ≥ b`;
   - roaster evidence: `own_vendor_share ≥ 0.5` **or** roastery wording on the home page;
   - catalog depth: `origins ≥ 2` **or** `roast_share ≥ 0.5`;
   - no doubt from rule 3.
5. **review.** Everything else with at least one coffee, plus an unsupported platform whose home page has roastery
   wording.

The candidate rule sets for validation are this one and three variants: without the depth clause, without the
`own_vendor` path, and #130's `own_brand`. Each is tried at N ∈ {2, 3, 5}.

### 4.3 Claude only on the undecided

- **Who it sees.** Review-tier places from rule 5 only (not rule 3's doubts), each once per `evidence_hash`, through
  Message Batches (`verdict.py`'s path).
- **What it sees.** `screen_definition.md` (section 5.1's definition, word for word) and the evidence: name, domain,
  Overture tag, `home_title`, 2,000 characters of `home_text`, and up to 40 products with title, vendor, type and
  sizes.
- **What it answers.** `roaster_online`, `not` or `unsure`, and one sentence.
- **What it can do.** Promote a place from review to suggested (`decided_by = claude`). A `not` keeps the place in
  review, sorted last, with Claude's sentence shown. Claude never drops a place, so it can't lose a roaster
  unseen.
- **The alternative.** Promote-and-demote is the other candidate, and validation chooses between them (D4).
- **The model** is `var('screen_verdict_model')`, set like `COFFEEDATA_VERDICT_MODEL`. The cheapest model that meets
  the floors on validation is chosen.

### 4.4 Output and confidence

`screen_scores` row: `roaster_id`, `tier` (`suggested` | `review` | `dropped` | `not_screened`), `decided_by`,
`reasons`, `rule_version`, `verdict_model`, `screened_at`, `next_due`. **Confidence is the tier**, and each tier
carries its measured precision from the sealed test with a Wilson interval (for example "Suggested: 96% [92, 98] on
test, 2026-12-…"). The admin shows that figure next to the status. There is no uncalibrated numeric score.

## 5. The evaluation (CLAUDE.md, in full)

The screen decides what the owner is asked to include, so it gets steps 2–7 in full. `main` suggests nothing:
precision is undefined, recall is 0, and there are 0 confidently wrong answers.

### 5.1 The written definition (`eval/roaster_screen/GUIDE.md`)

#130's `eval/screen/GUIDE.md` is carried over. Its label holds when all three of these do:
- **It roasts coffee itself.** The business, or the company that owns it under the same brand, roasts the beans. A
  roaster's own flagship café counts.
- **It sells that coffee by the bag**, whole bean or ground, by weight, under its own name, as
  `src/coffeedata/coffee_definition.md` defines a coffee. Samplers, pods and drinks don't count.
- **Online.** A person can put a bag in a cart on this site, or on a shop it links as its own under the same brand.
  Phone orders, in-person sales and marketplaces don't count.

Each item also gets a `kind`: `roaster`, `cafe`, `reseller` (including private label) or `other` (closed, parked,
not a coffee business, a chain's corporate site). `unsure` is allowed and is left out of the scores.

### 5.2 Items and reference labels

- **The population.** `candidates` not in the registry, on one Overture release, frozen by a hash of the frame (as
  #130 did).
- **Positives: the 210 curated roasters** (`sources` `curated` or `curated+overture` in `config/roasters.csv`). They
  are screened live, once: about 210 × 3.5 ≈ 740 requests to stores the crawl already reads weekly, on a
  screen day. They are labelled twice like everything else. A curated roaster labelled out of scope is reported as a
  seed finding, not dropped quietly. Their recall is reported **separately and unweighted**: they lean toward
  well-known, Shopify-heavy specialty roasters (128 of 210 are Shopify in the seed), so they are not the candidate
  population.
- **The candidate sample.** Stratified by Overture tag × screen tier. The tier is known before labelling, from the
  shadow run (section 8), so no extra requests are made. Each stratum is weighted by its size. Each set holds at least
  **100 suggested items**, so a precision near 96% has a Wilson lower bound near 90%.
- **Reuse of #130.**
  - #130's train and validation labels (200 + 200, label and kind) are reused for their sets. They were labelled
    against the same definition by two labellers with a third settling.
  - #130's test set was scored once, so it joins the regression fixtures (CLAUDE.md step 5), and a **new test set is
    drawn** from the test partition, leaving those 200 out.
  - #130's evidence is not reused. Every reused item is screened again by the new code in the shadow run.
- **The labellers** (D2). By default, two independent Claude subagents, as #130 used. They label from the businesses'
  own sites, blind to each other, the screen and the strata.
  - **The owner settles every disagreement**, and spot-checks 50 test items. A person settling matters here, because
    the screen's Claude stage and the labellers could share blind spots (CLAUDE.md step 6).
  - Two labels agree only when label and kind both agree.
- **Labelling cost** (an owner rule: ask before mass labelling or site visits). About 300 new test items and 210
  curated, labelled twice, plus about 25% to settle: **about 1,150 labelling site visits.**

### 5.3 Split

- **The rule.** #130's own, kept so that its train items stay in train:
  `n = int(sha256("coffeedata-screen-eval-2026-11-07:split:" + group)[:8], 16) % 100`. `n < 60` is train, `n < 80`
  validation, the rest test.
- **The group** is the registrable domain, so one business never sits on both sides. The curated 210 are assigned by
  the same rule.
- **Sealed test.** `SHA256SUMS` over `items_test.json`, `sample_test.json`, and the test evidence's hash, written when
  they are drawn. Nobody reads its examples while developing.
- **Scoring.** `python eval/roaster_screen/screen_eval.py final` prints only totals and intervals, as
  `eval/sealed/score_sealed.py` does.
- **The README** records the rule, the salt, and the frame hash.

### 5.4 Protocol and metrics

- **Train** is the only set read: thresholds, the `named` and brand rules, and the Claude prompt are tuned there.
- **Validation** chooses among the candidate rule sets (section 4.2) and the Claude role (D4), on aggregates only:
  the highest suggested-tier precision subject to the floors below, with ties going to the stricter rule. Then the
  choice is frozen.
- **Test** is scored once: `main` and the change, on the same frozen set.

All metrics are stratum-weighted, with Wilson 95% intervals on the labelled counts:
- **Precision of suggested:** in scope / suggested.
- **Recall:** suggested / in scope, and (suggested + review) / in scope; curated recall separately.
- **Confidently wrong:** a suggested place labelled `cafe` or `reseller`, as a share of suggested; and a dropped
  place that is in scope, as a share of dropped.
- **Review load:** review-tier places per 1,000 screened, which is the owner's time.

### 5.5 Ship criteria (D3 sets the numbers; these are the defaults)

| Metric (test) | Bar |
|---|---|
| Suggested precision | ≥ 95%, Wilson lower bound ≥ 88% |
| Suggested confidently wrong (café or reseller) | ≤ 3%, upper bound ≤ 8%. #130's admit rule was 10.6% |
| Dropped that were in scope | ≤ 2% of dropped |
| Recall, suggested | ≥ 40% (#130: 37.5%) |
| Recall, suggested + review | ≥ 75% |
| Curated recall, suggested + review | reported, with no bar (biased sample) |

Below the precision bar, nothing is labelled "Suggested". The tiers still exist and everything goes to review.

### 5.6 Keeping the bar

- **Floors.** `tests/test_roaster_screen_accuracy.py` holds floors on train and validation (precision, confidently
  wrong, recall), as `tests/test_classifier_accuracy.py:23` does. They are only ever raised.
- **Unit tests** keep the named cases:
  - Germack (a fixture page invented for the test, not fetched);
  - Rival Bros;
  - a café that "proudly serves" a known roaster;
  - "The Counter" selling Counter Culture;
  - a wholesale-only store;
  - a chain.
- **The running total** (step 9) lives in `eval/roaster_screen/README.md`: the places whose tier changed since the
  last full evaluation.
- **Production check.** The owner's actions on suggested places are counted per `rule_version`. Accept counts as
  agreement; Not a roaster counts as a miss. If misses exceed 5% of 100 or more actions, the admin stops showing the
  Suggested status, falls back to review, and says why. These actions are train-only labels: test and validation
  items are left out and counted, as `decide export-labels` does.

## 6. How results reach Sources (#131)

- **Status.**
  - `SourceList._make` (`admin/sources.py:120`) joins the served release's `screen_scores`. An **unflagged** place
    gets the status **Suggested roaster** (tier `suggested`), **Needs review** (tier `review`), or stays
    **Unreviewed**.
  - Dropped and not-screened places show their reason as a hint, for example "screen: no coffee for sale, 2026-12-03"
    or "screen: robots.txt disallows".
  - The owner's flag always wins over the tier. Inherited registry roasters are not screened.
  - The status sort puts Suggested first, then Needs review. The Filter panel gains `screen` (tier, reason) and the
    rule's measured precision.
- **Bulk accept.**
  - Filter to Suggested (and a state), select all, and choose **Accept as roasters**.
  - A preview, like #135's, shows the count, the states, 20 sampled rows with their `reasons` and links, and the
    tier's measured precision. The save needs the preview's token for exactly that selection and the served release.
  - The write is `flag_sources` (`admin/controlplane.py:443`) with flag `roaster`: 1 to 1,000 places per batch (the
    existing limit, `:458`), CSRF-checked, idempotent on its key, and audited in the same transaction.
- **Audit.**
  - A new migration (the next free number; #135 takes `0005`) adds `evidence jsonb` to `control.source_flags`. It is
    written only by bulk accept: `{screen_id, rule_version, tier, decided_by}`, with `client = 'screen'`.
  - The Log page shows each batch and its evidence. **Undo** reverts the batch (`undo_flags`, `:501`).
  - Each accepted place's Case shows the screen's evidence: the products read and the reasons.
- **To the crawl: unchanged.** Flags reach `registry.current` only in a published draft's snapshot. **Pull now** can
  probe and crawl accepted places that week. The admin reads the lake only, as #131 does.
- **Review** is the same page with the Needs review filter. Each row shows its reasons and Claude's sentence, if
  there is one, with Roaster, Not a roaster, and Open site.
- **The site visit.** Open site happens in the owner's own browser, never from the admin service.

## 7. Cost

| Item | Estimate |
|---|---|
| Requests | About 155,000 over about 8 weeks (about 10,000 per run, 2 runs a week). Each host gets about 3.5 requests once, then at most one re-screen in 6 to 12 months. For scale, the weekly crawl reads 3,200 stores' full catalogs every week |
| Claude | Review tier only. Expect about 3,000 places (a rough guess from #130: coffee found at about 12% of candidates, half of them undecided) × about 3,000 input and 150 output tokens ≈ 9M in and 0.5M out per full pass. At the price `docs/app/PLAN.md:593` cites for the verdict model's tier (0.10 / 0.50 dollars per million), that is about **$1**, and less in batches. At ten times that price it is still under $15. Re-screens add little |
| Cloud Run | One job, 2 vCPU and 2 GiB, at most 90 minutes, twice a week: about 21,600 vCPU-seconds a week. A few dollars over the whole program at list price (check current pricing). The dbt models add seconds to the Tuesday build |
| Lake storage | Products are read for about 15,000 places × about 60 products × about 2 KB ≈ 1.8 GB raw, about 0.4 GB as Parquet; `home_text` about 0.2 GB. Private dataset |
| Evaluation | About 1,150 labelling site visits (section 5.2) and about 740 requests to screen the curated roasters live. The shadow run is the production screen itself |

## 8. Risks

| Risk | What happens | Mitigation |
|---|---|---|
| Café resellers selling another roaster's bags | Rules see coffee in bags and may suggest them | A suggestion needs its own name on the bags or roastery wording on the home page; another roaster's name, 3 or more vendors, or a "serves" phrase forces review; Claude can't promote a doubt; confidently wrong is a ship metric. Private label with a blank vendor is the hardest case: it lands in review unless there is home-page wording |
| Multi-location chains | One domain is one row (`candidates` dedupes); more than 20 stores is a chain (#149) | A chain can be suggested. `is_chain` keeps it out of public tables. Corporate sites with no shop are dropped |
| Wholesale-only sites | Login walls or "wholesale" catalogs | `wholesale_share ≥ 0.8` goes to review; a password page (Shopify's `/password` redirect, `http.unavailable_reason` `:317`) is dropped as closed |
| Closed or parked sites | DNS failure, parked pages, empty catalogs | Dropped with a reason; re-screened after 180 days |
| Blocked edges (Cloudflare, SiteGround's 202) | No evidence | `not_screened` with the refusal recorded; never worked around. A run with more than 8% refusals stops. SiteGround's default-egress challenge means its hosts mostly end `not_screened` (D6) |
| The first page is not representative | Merch first, coffee on page 2 | A full page (`page_full`) with no coffee goes to review, not dropped, unless it has 50 or more products |
| Coverage | Unsupported platforms: about 20% of the seed is Wix or "other" | Home-page wording sends them to review; Squarespace and Square Online come in phase 2 |
| Evidence ages | Labels and screens drift apart | Labels are made within 14 days of the shadow screen; stale suggestions re-screen at 180 days |
| Load on the crawl | Shared IPs, shared Shopify limits | Default egress; Wednesday and Thursday only; the in-process Shopify limiter; its own job, so its own window |
| Third-party data | Listings of places not in the registry | A private lake only, never exported, `home_text` capped |

## 9. Rollout in small PRs

1. **Eval scaffold** (low). `eval/roaster_screen/`: GUIDE, README (rule, salt, frame), the #130 labels imported for
   train and validation, #130's test moved to fixtures. No code.
2. **Raw tables and the job, record-only** (high: new lake tables).
   - `lake.py` schemas, `screen.py`, the `probe_one` → `_probe(page_size)` refactor with a parity test, and
     `coffeedata screen`.
   - Fake-transport tests: robots refusal, challenge, cap of 6, deadline, re-screen windows.
   - dbt staging and contracts. No schedule.
3. **Features and rules to `screen_scores`** (high: dbt gates). `screen_rules.py`, `int_screen_*`, `screen_scores`,
   the tests, a full `coffeedata build`. Provisional thresholds.
4. **The job in terraform, unscheduled** (high). The `screen` entry in `local.ingest_jobs`; the owner applies it and
   starts the **shadow run**: about 3,000 places in MI (Germack's state) plus a hash sample across states, then the
   curated 210. Nothing is shown in the admin.
5. **Claude stage and the evaluation** (high).
   - `screen_verdict.py` and `screen-verdicts`.
   - Labels (after the owner approves the labelling cost), the validation choice, the freeze, and the sealed test
     score in `RESULTS.md`.
   - Floors.
6. **Admin: Suggested, bulk accept, audit** (high: publishes data), on the #131 stack. Migration, preview, Log,
   Playwright at 390 px and desktop, light and dark, a slow server, errors, axe.
7. **Schedule, state by state** (medium). The Wednesday and Thursday schedule entries and the wave list; a weekly
   summary line in the run's `job_manifests`.

Each PR follows "one review, fixes, one", and `make smoke` passes on each.

## 10. Open decisions for the owner

| # | Decision | Recommended default | Why |
|---|---|---|---|
| D1 | Fetch one product page per candidate to *suggest* roasters, which reverses "no site is fetched to decide what to crawl" (2026-11-07) | **Yes**, as agreed, with the flag kept as the owner's | Recall without it stays at 46%; the owner still decides each place |
| D2 | Labellers | **Two Claude subagents; the owner settles disagreements and spot-checks 50 test items** | Cheap, and a person breaks the shared blind spots with the Claude stage |
| D3 | Ship bar | **Suggested precision ≥ 95% (lower bound ≥ 88%); confidently wrong ≤ 3%; dropped-in-scope ≤ 2%; recall ≥ 40% suggested, ≥ 75% with review** | Bulk accept is only safe when nearly every row is right; #130's 10.6% wrong was too high for that |
| D4 | Claude's role | **Promote only** (review → suggested); never drop | A demotion error loses a roaster unseen. Validation can still pick promote-and-demote if it measures better |
| D5 | Waves and pace | **MI first (Germack), then the largest states first; 2 runs a week × 3,000 places; about 8 weeks** | Slow and visible; Germack's state checks the named case first |
| D6 | Egress | **Cloud Run default egress**; SiteGround-hosted places stay `not_screened` | Keeps the crawl's NAT IPs clean; the owner can flag those by hand |
| D7 | Platforms | **Shopify and WooCommerce in phase 1**; Squarespace and Square Online after the first test score | One request each, and most curated roasters (128 of 210 on Shopify) |
| D8 | Re-screen windows | **28 days transient, 180 days dropped or unacted, 365 days refused** | Respects a site's "no" and keeps the program to about one pass |
| D9 | Reuse #130's labels and split | **Yes** for train and validation; a new test set | Saves about 800 labels; #130's test is spent |
| D10 | Stored evidence | **Product text capped at 1,500 characters, home text at 10,000, private lake only** | Raw is immutable, so the cap is set now; enough for the rules and the labels' audit |

**Germack, the named case.** In the MI wave, check three things in order:
1. Is `germackcoffee.com` in `candidates`? Its Overture record was never looked up. If it is missing, the screen
   can't find it, and the add-roaster form is the route.
2. Its `screens` status and platform.
3. Its tier and reasons.

Report all three in PR 4's notes, whatever the result.
