# Next steps when the hold lifts (written 2026-11-04 ~02:45 UTC)

The owner said "Hold everything" at about 02:36 UTC. Nothing moves until they lift it. The durable copy of this list
"⏸ On hold" section on GitHub. Paths under `scratchpad/` are container-local and are lost when the container goes:
recreate them (see "Container-local files" below).

## Standing rules (owner)
- Only major bugs block merge. Each PR gets one fix round plus one re-review.
- **Skills are mandatory:**
  - dbt skills on every dbt change;
  - design skills on every website change;
  - DevOps skills on every DevOps change (terraform, jobs, workflows, alerts, runbooks).
- Point every reviewer at `scratchpad/SKILLS-INDEX.md` (local copies). Reviewers must cite the skills they used.
  SearchSkills/ListSkills don't find them.
- **Don't poison a review:** give the reviewer only the owner's question.
- **Merges:** merge commit (repo convention), only after CI is green on the head. Take the PR out of draft
  (`update_pull_request draft=false`) right before merging, and only once the hold is lifted.
- **Tests:**
  - HF_HUB_OFFLINE=1;
  - dbt on PATH (`scratchpad/venv/bin`);
  - the three keys unset;
  - an own `--basetemp` inside the worktree.
- **Never:** print secrets or env vars, write to the remote lake, or run terraform plan/apply or gcloud.
- **Status updates to the owner:** C.O.R.E. format, concise.

## Merge order
#104 → #93 → #106 → #107 → #108. #105 any time; it's needed before Monday 2026-11-09 for cutover2.

## Per PR

### #104 tag parity warn: branch `tag-parity-warn`, head 2f61719 (WIP, untested)
1. Full suite on 2f61719: ruff, format, mypy, pytest.
2. Merge origin/main (has #82) and resolve.
3. Re-review with the dbt skills, plus DevOps/runbook skills for the RUNBOOK edit.
4. CI green, then ready, then merge.
5. Follow-up issue/agenda: decode tags in `coffeedata.products.item`, or the warning never clears (finding 3).

### #93 fetch-health: branch `post-crawl-dq`, head bd4a84a (WIP, untested; main already merged)
1. Full suite plus terraform fmt and validate.
2. Re-run the 2026-11-02 replay on the read-only copy (`scratchpad/dq-week/lake`, `lake-fetches`) and rewrite the PR's
   Replay section and the challenge row of its mapping table. Expected: the bare-202 `other` bucket no longer systemic.
3. Reply on the review thread (issuecomment-6620744991) with what changed.
4. Re-review with the dbt and DevOps skills. Check the MAJOR fix holds: one challenged store must give no alert.
5. CI green, then ready, then merge. Owner steps are in the PR body: image, plan of 4 add / 12 in place, a trial
   `--force` run.


**Owner decision 2026-11-04 (supersedes the challenge work above):** a 202/challenge is an ordinary failed fetch, so
the week's later passes retry it. Drop the `challenge` systemic scope and `edge_challenge_share` from #93; challenged
stores stay on the list as `failed` (note the edge if known). No edge alerts, and no new thresholds. Separate small PR
in about two months (early January 2027): a store with the same challenge for **6 consecutive weeks** (owner, 2026-11-04; keep it a variable) is marked blocked,
normal crawls skip it, and it moves to a different crawl method. #93's fix round is then: test it, publish the
verdict (R1/R2), suspects to `as_of_run`, the overlapping-poll guard, the dead-attempt rule, and the nits. Nothing
more.

### #106 Snowflake upper-case views: branch `snowflake-uppercase-columns`, head 9de2546 (tested)
1. Check that CI on 9de2546 is green, then ready, then merge.
2. Tell the owner:
   - retype the 4 sample queries from `marketplace/LISTING.md` into the listing;
   - run `coffeedata load-snowflake --dry-run` before Tuesday;
   - after the first load, check `show grants to share coffee_data_share` and `desc view SHARE.COFFEE`;
   - it's a breaking change for consumers' quoted lower-case queries.
3. The listing Publish stays on hold until currency (#107), non-coffee (#108 plus backfill) and duplicate roasters are
   fixed, and the listing text drops its "USD" claims.

### #107 currency: branch `store-currency`, head 8f30b72 (WIP, untested; main merged; contract 2.4.0)
1. Full suite in `/home/user/coffee-store-currency`, plus pre-commit. Fix any failure.
2. Reply on review issuecomment-6620673564: one line per fix (MAJOR 1, MAJOR 2, findings 3 and 4).
3. Confirm with the owner: a price with no stated currency from a US-listed roaster counts as USD. Their "USD," reply
   was cut off. If they want stated-USD only, change the `is_usd` macro and redo the step 9 count.
4. Re-review with the dbt skills. Check the two MAJORs are reproduced-then-fixed: a pre-1.6.0 lake builds, and a
   first CAD crawl with a sold-out bag passes `public_prices_are_usd`.
5. CI green, then ready, then merge after #106.
6. Next in the listing-fix order: duplicate roasters (35 groups, about 927 coffees).

### #108 verdict guard: branch `verdict-guard`, head 23e9a68 (tested before main moved)
1. **It conflicts with main.** Merge origin/main in, with no rebase. The public contract can't be 2.2.3: take the
   next free version after the PRs ahead of it (#82 = 2.3.0, #107 = 2.4.0). Use `make contracts`, never edit by hand.
2. Full suite.
3. Review (never started; the first was stopped by the hold): the dbt and DevOps skills. Questions:
   - Does a correct production run pass?
   - Is the 0.85 floor justified, or would it fail a good week?
   - Does the first build before the backfill fail as intended?
4. CI green, then ready, then merge last.
5. **Owner:** `cutover2.sh` sets `verdict_model = "claude-haiku-5-5"`. Then the backfill: classify `--wait`, collect,
   rebuild, `rollback_to=served`.
   - Record the changed-product count in `eval/sealed/README.md`. Predicted: about 3,600 out, about 700 back.

### #105 second NAT IP: branch `woo-generic-nat`, head 684b72e (tested, re-read)
1. Check that CI on 684b72e is green, then ready, then merge, so the owner can cut over before Monday 2026-11-09.
2. Owner:
   - run `scratchpad/cutover2-paste.txt`, which has the tightened guards;
   - run it Tuesday after 12:00 UTC through Sunday;
   - then the SiteGround ticket with `woo_generic_egress_ip` (RUNBOOK §2.1).
   - **Raise `budget_usd` to about 30** (owner's call).
3. Non-blocking follow-ups:
   - a shared `rps` budget on the one IP;
   - the NAT's per-GB charge in the DESIGN cost;
   - a test tying `Connector.egress` to `ingest_jobs`;
   - `ingest_subnet_cidr` validation.

### #82 (merged as 22e6e00)
1. Re-run the post-merge dbt-skills audit (stopped by the hold). Post findings on #82, and put any fix in a follow-up PR.
2. Non-blocking items from its review, for Wednesday:
   - `run_manifest_mode_values` has no override;
   - the freshness rule is symptom-only (scope the run tables to `as_of_run`);
   - `public_tables_are_one_release` can't fail in a build;
   - price-event invariants are checked on the public table only;
   - `filled_share:price_per_100g` moves with stock.

## Open questions for the owner
- Keep or pause the Wednesday 15:00 UTC session trigger (trig_01P1GFTjrgN9Abn2qMaRKSDy)?
- Release #105 from the hold before Monday?
- Rotate the HF and Snowflake keys printed in a subagent log earlier (asked, no answer).

## Housekeeping
- Remove the diag workflow and script from session branch ccr-53fdc95c-2qdy7y: `.github/workflows/sg-diag.yml` and
  `tools/diag/siteground-tls.sh`.
- Cleanup record and restore steps: `scratchpad/cleanup/README.md`.
- Worktrees still present:
  - `/home/user/coffee-store-currency` (#107);
  - `/home/user/vg` (#108);
  - `scratchpad/wt-fh93b` (#93);
  - `scratchpad/wt104` (#104);
  - `scratchpad/pr108`, `scratchpad/wt108review`, `scratchpad/wt` (read-only, removable).

## Container-local files: lost with the container, and how to get them back
- **`scratchpad/SKILLS-INDEX.md` and the skill copies (`scratchpad/skills-src/`, `scratchpad/ext/`).** Re-clone the
  sources: dbt-labs/dbt-agent-skills, great-expectations (`.agents/skills`), duckdb skills, motherduckdb/agent-skills,
  wshobson/agents, the terraform-skill, hashicorp/agent-skills, adewale/good-readme, hidai25/readme-roast,
  Owl-Listener/designer-skills, jakubkrehel/skills, emilkowalski/skills, elayadesign/ai-design-skills and MengTo/Skills.
  Then rebuild the index by area: dbt and data quality, DevOps, docs, design.
- **`scratchpad/cutover2-paste.txt`** (the owner's cutover script with the tightened plan guards). Its guard block is
  in the #105 review comment (issuecomment-6620550801), under "Exact edit". The rest of the script is in the
  session transcript.
- **`scratchpad/dq-week/lake*`** (the read-only lake copy). Download again read-only from
  `hf://datasets/jtouley/coffee-data`, and never write to it.
- **`scratchpad/cleanup/` backups** (the removed worktrees' patches and bundles). Every branch is still on GitHub or
  in the PRs. The 3 local-only merge commits were merges of pushed branches, so they can be recreated. The rest is
  lost with the container.
- **Uncommitted work: none.** Every PR branch is pushed.

**Owner policy, restated 2026-11-04:** don't be lenient with sites that block access to public catalog data. Keep
retrying every pass and every week (the 5-minute wait cap still applies). Blocked status only after 6 consecutive
weeks of the same challenge, and then switch crawl method rather than give up. The blacklist PR is a follow-up for
about two months from now; GitHub issue #110.

## Owner decisions, 2026-11-04 (evening)
- **Green (unroasted) coffee is not coffee.** This matches `eval/sealed/GUIDE.md`, which excludes it already; no
  definition change. So Coffee Bean Corral's green coffees were removed correctly. copantrade's 22 green pallets that
  are still public are misclassified: flag them, don't hand-remove them.
- **Removals need flags from several signals, plus admin approval.** No more hand-edited CSVs or code lists for
  removals. The plan is an append-only `decisions` table:
  - states: flagged → approved → applied / rejected;
  - pipeline signals raise the flags, and a removal needs at least 2 of them or a human-confirmed live check;
  - dbt applies approved decisions softly: internal tables keep the rows, public tables filter them out.

  The design proposal comes first; the owner picks the approval surface (an issue form or an admin page in the app).
  Withdrawn: the "overrides PR" (Stivers restore, 3 mixes, 8 registry fixes). It waits for this mechanism.
- The validator's per-roaster calls were wrong for Chuck's Roast and Taybat: they sell coffee, and we fail to crawl
  them (Chuck's sends our bot into a redirect loop; Taybat's feed returns 403). Any registry removal needs a live check of the store.

## Restart plan (Friday reset)
The live restart page is https://claude.ai/artifact/T8CXDYCf2Tsv2mrU4Rq7S8 (section "Restart on Friday"). Draft PRs: #93, #104, #106, #107, #108, #109, #111, #112, #113. Merged: #82, #105. Release 5060ad exported 2026-11-04.

## Owner answers on the #112 open questions (2026-11-04, late)
1. Guard scope: the owner found it unclear. Re-explained as "when Claude flips a published coffee to not-coffee, remove it unless something looks suspicious (recommended), or hold every such removal for review (~3,765 in the backfill)". Awaiting an answer.
2. Restores and crawling: the owner wants a per-roaster crawl flag in the admin. Unchecking it stops crawling that roaster. Checking it allows crawling, and for a new or under-covered roaster it runs a single-roaster test crawl (`crawl --only`) as a smoke test, followed by a draft build. Restoring a single coffee stays a decision, published through the same flow.
3. Sign-in: no binding to Gmail; the project sits on a personal Gmail with no Workspace or organization. So no IAP. Use the review's fallback: passkey-only sign-in in the admin (two passkeys plus recovery codes, no password, 12-hour revocable sessions), and CLI device tokens in the OS keychain.
4. Rule-config storage: the owner asks "should all be code?" and wants a platform-engineering review. Launched; it posts on #112.
5. Green coffee by the sack: deferred. First measure how confident the verdicts are without extra rules. The owner mentioned a decisions API ("jev.ai"); not yet researched.
6. Group approval: the owner found it unclear. Re-explained as "approve all 16 Stivers coffees from one check of the store, instead of 16 separate checks". Awaiting an answer.
7. Stale drafts: rebuild automatically, smoke-test what changed and run the test suite. Publishing stays the owner's explicit action, through an explicit workflow, never automatic.
- Platform-engineering answer on Q4 (https://github.com/jtouley/coffee-data-tracker/pull/112#issuecomment-6641959444):
  - **Rules are code** and **decisions are data**. Rule config goes in git: `config/rules.yml`, with a JSON Schema generated from the code and checked in CI. A PR is the draft, and merging plus an approved deploy is Publish.
  - Per-item decisions, including the roaster crawl flag, go in the private HF curation dataset. Each build reads a pinned `@<commit>` and records it. A weekly mirror goes to a versioned GCS bucket, and the build fails if the dataset can't be read.
  - No Postgres while there is one curator.
  - This supersedes the design's "rule config in the dataset".

## Friday: control-plane POCs (owner, 2026-11-04)
- The owner overrules the platform review: settings edited in the admin panel need a **database-backed control plane**, which doubles as the **data catalog** (rule config versions, decisions and flags, roaster state and crawl flags, DQ results per build, table and contract metadata). Keep from the review:
  - code defines the rule types and the settings schema;
  - every change is a new version row;
  - each build pins an immutable snapshot exported to the lake, so it never reads the live DB.
- POCs to run:
  1. **Database:** Cloud SQL (smallest, IAM auth, PITR, ~$9-10/mo) against free tiers: Neon, Supabase, Fly.io Postgres, CockroachDB Serverless, Azure SQL free tier (100k vCore-s, 32 GB, auto-pause), Oracle Cloud free MySQL. Judge on cost, IAM/no-password auth, backups, egress to Cloud Run, cold-start latency, and lock-in.
  2. **Language:** Go vs Python for the admin/control-plane API. Go: a single static binary, small image, fast cold starts, strong typing. Python: reuses `coffeedata` (draft build, build-diff, serve) and dbt directly. A Go control plane would shell out to or call the Python build. Build the same thin slice in each: decision write, version row, snapshot export, trigger a draft build.
  3. Measure each POC on the same slice; the owner decides.

## Shot and bag matching gaps (found 2026-11-04, for Friday, before Dec 1, Onyx advent)
- **The matcher ignored the process it read.** The photo read "Counter Culture / finca el puente / Honey Process". There are 6 Finca El Puente listings, and the matcher picked "Gesha Variety" (`7050528194637`, score 92) instead of "Honey Processed" (`7610173063245`). Fix: break ties on the process, variety and origin read from the label. It's a decision change, so it gets the CLAUDE.md evaluation.
- **Bag corrections don't reach earlier shots.** The bag was fixed on 10-31, but the 14 shots from 10-29 keep the free text. Fix: resolve each shot's coffee through `bag_id` in dbt (latest bag version), and stop copying the coffee at log time.
- **September Coffee Company (Canada)** isn't in the US registry, and its photo has no label read. The personal log needs off-registry coffees that still count for preference modelling.
- **Tasting notes.** All 6 Counter Culture Finca El Puente listings have 0 notes, and only 21% of the catalog has any. The bag-photo read doesn't extract tasting notes; add them, store them with the bag, and fall back to them when the catalog has none.
- Also add a per-coffee rating, plus a predicted rating logged before opening, separate from shot ratings. Shot ratings swing 2-5 within one coffee because of dial-in.
- Product order (owner + ChatGPT): insights, then predicted taste, then recommendations, then preference optimization. Insights are deterministic dbt marts; the LLM only explains findings the mart computed. Every insight shows its n.

## CRITICAL user-facing: invites and sign-up (beta tester report, 2026-11-05)
- What happened: the tester tapped "Create account" and saw no response. The account was in fact created; a retry then said "code already used". Cause: a Cloud Run cold start (scale to zero takes minutes) with no feedback on the page.
- Sign-up: disable the button on submit, show a "Creating your account…" spinner, then an explicit confirmation and error states. Make it idempotent, so a double tap can't use the invite twice.
- "Code already used" right after this person redeemed it: say so and link to sign-in.
- Invite (join) link: serve a static loading page that polls /health while the app wakes, then continues.
- Owner's invite creation: loading and confirmation states, plus copy feedback.
- Cold start: measure it, then decide between min_instances=1 and a faster startup.
- Responsiveness on phone widths. Add an end-to-end join-flow test, including a slow-server case.
- More tester detail: he is a senior UX designer and built the iOS app Dialed Grounds; he's open to integrating with the dataset.
  - Instagram's in-app browser showed only a Meta Pay sheet. Detect in-app browsers, show "open in Safari/Chrome", and test the flow inside IG.
  - The username rule `[a-z0-9_-]{3,32}` (app/accounts.py:36) rejects emails and dotted names, and the page doesn't show the error. Accept emails or dotted names, or state the rule up front and show inline errors.
  - A password from a password manager, and later a simpler one: the submit button seemed dead. Every validation error must be visible inline. Test with a generated password.
  - His ask, verbatim in spirit: "fine if it's slow, just disable the button and show it's in progress".
- Owner decisions (2026-11-06) for sign-up validation:
  - State the username and password rules up front, under each field.
  - Live checks as you type, with a ✓/✗ per rule and the reason in words.
  - The server keeps identical checks and returns the same messages.
  - Passwords must be compatible with 1Password's generator (https://1password.com/password-generator) at the owner's setting: Random, 20 chars, numbers and symbols on (e.g. `)]=_:!%`). Also cover Memorable (words with separators).
  - Add fixtures for every symbol 1Password can emit, and an end-to-end test that they survive form encoding and JSON.
  - The current rules (10–256 chars, not common, doesn't contain the username, ≥4 distinct chars) should already accept such passwords. The tester's failures were probably the username rule `[a-z0-9_-]{3,32}`, which rejects `@` and `.`, combined with errors that never appeared on the page. Verify this with tests.

## Review policy (owner, 2026-11-06), supersedes earlier review loops
For every PR:
1. one specialist review, with the relevant skills (dbt/DQ, DevOps, design, security);
2. one round of fixes;
3. one final review.

If that final review finds nothing critical, wait for CI and merge. No more review rounds.

## CI smoke test (to design)
A fast smoke job across web, ingestion and dbt, as a required check that runs in a few minutes. The full suite takes ~20 min. Plan proposed 2026-11-06; waiting on the owner.

## Frontend rule (owner, 2026-11-06)
Every frontend change (site, app, templates, CSS, JS) is driven end to end with Playwright before review and before pushing, the same way every dbt change runs a full build:
- every state the change touches, at phone and desktop widths, in light and dark;
- slow-server and error cases;
- an axe check.

The design-skills specialist review reads those screenshots. Lesson: the sign-up and invite problems the tester hit would have been caught if this had been done before.
- Review policy, addition (owner, 2026-11-06): every specialist review runs /code-review at high effort on the full PR diff, plus its skills. It posts one full review as a PR comment, with each finding tagged by severity and source (the /code-review run or a named skill).
- Review policy, addition 2 (owner, 2026-11-06): "Verify the premise first" goes in each review, and into CLAUDE.md via the smoke-test PR.
  - Steps: existing work; the real failure (logs, not summaries); reproduce it with the exact output and exit code; primary sources; who can fix it; only then act, and show the owner any public post outside the repo first.
  - Each review's PR comment ends with "Premise checks": what was checked, what was found, what was not verified.
- /code-review levels (owner, 2026-11-06):
  - low: docs, specs, copy;
  - medium (default): ordinary features;
  - high: auth/security, money, anything that publishes data (dbt gates, public tables, contracts), terraform;
  - final review after fixes: low or medium.
  Goes into CLAUDE.md via the smoke-test PR.
- 2026-11-06 #120: the security plus /code-review specialist review found no critical issue and 3 MAJORs:
  - Change password posts to the wrong API;
  - username enumeration on join without a valid code;
  - renewing a reset link returns 500.
  The fix round is running, with the cheap minors too. m7 (reset links last 7 days; OWASP suggests 24–72 h) is left for the owner to decide. Next: a final low/medium review, then CI, then merge.
- Owner decision (2026-11-06): reset links stay valid for 7 days (m7 closed, no change).
- Rule (owner, 2026-11-06): every review agent posts its review to GitHub as a PR comment. The three #120 design reviews were backfilled as comments 6688494016, 6688494429 and 6688494822. Posting needs the REST API (gh api …/issues/N/comments), because gh pr comment uses GraphQL, which is blocked.
- 2026-11-06 #93: the final review is clean (issuecomment-6688842599). Merge on green CI once the PR is out of draft (the owner clicks Ready). Follow-ups:
  - give fetch-health a ~1800 s task timeout;
  - a still-running manual --only run is marked dead when a later pass starts;
  - remove the dead mode==only branch;
  - #105 left DESIGN §6 saying the non-Shopify crawls retry once.
- 2026-11-06: #107 merged (47e3356). Follow-up minors:
  - fct_price_event dbt unit tests with cross-currency rows;
  - contract wording on previous_price_usd;
  - CHANGELOG entries for public 2.4.0 and raw 1.6.0;
  - README / marketplace README still say 45,000.
- 2026-11-06 #120: the final review is clean (issuecomment-6689574646). Merge on green CI once it's out of draft (the owner clicks Ready). Follow-up minors:
  - m1a: a timing tell inside the 15-min retry window (the limiter refuses before the password check);
  - m4a: when the 'Make another link' confirm times out, the For box stays open over the old link.
- 2026-11-06: #93 merged (3ad007a) and #120 merged (f7ba6e4). Owner: don't wait for confirmation when the review loop is complete. Mark the PR ready and merge it myself after a clean final review and green CI.
- 2026-11-06: #121 merged (ceddc26): the smoke CI plus the CLAUDE.md rules. Owner action: make `smoke` a required check in branch protection.

## Owner TODO for the next shell script: branch protection on main (deferred 2026-11-06)
The session proxy can't write rulesets (HTTP 403), so the owner adds this to their script and runs it with their own gh login:
```bash
gh api -X POST repos/jtouley/coffee-data-tracker/rulesets --input - <<'JSON'
{"name":"main","target":"branch","enforcement":"active",
 "conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}},
 "rules":[{"type":"deletion"},{"type":"non_fast_forward"},
  {"type":"pull_request","parameters":{"required_approving_review_count":0,"dismiss_stale_reviews_on_push":false,"require_code_owner_review":false,"require_last_push_approval":false,"required_review_thread_resolution":false}},
  {"type":"required_status_checks","parameters":{"strict_required_status_checks_policy":true,"required_status_checks":[{"context":"smoke"},{"context":"test"}]}}]}
JSON
```
It restricts deletions, blocks force-push, requires a PR (0 approvals) and requires `smoke` and `test`, with no bypass list (the owner can always edit it). Later: bind the deploy WIF credential to a protected `deploy` environment (terraform PR).
- 2026-11-06: #122 merged (632f415): fixed the cross-PR smoke break. #108 merged (8b54f80): public contract 2.5.0, verdict gate at 85%, terraform requires verdict_model. All Monday-critical PRs are on main, so cutover2.sh is ready to run.
  - #108 follow-up minors are in issuecomment-6690379936 (items 2-11).
  - The contract version was hand-edited because no generator exists for the public contract; consider adding one.
- Owner decision (2026-11-06): the ruleset requires up-to-date branches (strict=true; the command above is updated). Priorities now: #111, #113, #112.
- Owner decision (2026-11-06, #112): guard scope = remove, but admin can review any removed coffee and flip it back (per-coffee override on coffee_id/hash, versioned, feeds next draft). Group approval = yes for evidence-matching members; plus all coffees browsable in admin with keyword search and filters on dataset values (Phase 1).
- Owner decision (2026-11-06): privacy policy update = option (b): one update shipped WITH the email feature + legal review (single /agree re-acceptance). Scope: terms-acceptance record, invite "For" note, email field (encrypted, pre-ticked marketing opt-out), anonymized insights sold to roasters (wording to confirm; must land before any data is sold), personalised recommendations from logged bags, fix "address on your account" line. Bump LEGAL_UPDATED.
- Owner decision (2026-11-06, #112): keep `non_coffee_type` (listing-family rule) in Phase 1; its type list must be measured on held-out data per CLAUDE.md before shipping, and check overlap with the rules classifier.
- 2026-11-06: #113 merged (18671d0). Open owner question: verdict tie → alphabetical label (deterministic) vs prefer "coffee". #113 MINOR follow-ups listed on PR (issuecomment-6692873868).
- 2026-11-06: #123 merged (669f1d1): verdict ties prefer coffee; not_coffee/unsure tie stays not_coffee (owner default A). #111 needs origin/main merged again before merge (strict).
- 2026-11-06: #111 merged (e992120). Priorities #111/#113/#112: two merged, #112 draft until Friday POCs. cutover2.sh image will now include #111 (report-only reconciliation step), #113 (UTC pin, hashes), #123. Follow-up: reconciliation marks every row changed when served release lacks a compared column.
- Owner decision (2026-11-07): build the whole admin (control plane + command center), no read-only stopgap (no MotherDuck/BI/MCP). Started: PR command-center-exports (build-side exports, git sha, job manifests, reexport action; merge only AFTER cutover2.sh); local Go-vs-Python POC now (DB half needs owner-created resources). Crawl-side data fixes (structured fail reason, pass slot, Claude spend) after Monday crawl. SiteGround 202 gap = fixed on main (PR #98, 11-02 21:34), not in the 11-02 image.
- 2026-11-07: #124 (command-center exports) approved by final review; HOLD merge until owner runs cutover2.sh, then merge before Tue build. Owner: add GIT_SHA build-arg to ~/cb.yaml.
- Owner decisions (2026-11-07): merge #106 (after main merge + skills review); start security P0s (photo no-store, auth fail-closed) as PR app-hardening-p0 with Playwright + design walkthrough; closed #71/#67/#63.
- Owner rule (2026-11-07): restate every open decision in every update, each with a simple why.
- Owner decision (2026-11-07): admin language = Python, revisit if real Cloud Run cold start p95 > 2 s; add a Cloud Monitoring alert for that with the admin service (in #112 Phase 1).

## 2026-11-07 (later)
- Merged #126 (app hardening: default-deny auth, no-store on private responses), deployed from main. Merged #106 (Snowflake upper-case views).
- Owner: accepted the offline Back change from #126; the weekend Bellingham check counts as its 11-09 crawl.
- not_coffee/unsure tie: closed, no change (no such ties exist; any wrong removal is fixable in the admin).
- #125 (admin) fixed after final review (owner-lockout regression); held behind #124, which waits on cutover2.sh.

- 2026-11-07 (owner): scope is who sells coffee where (roasters and bean sellers). The cafe -> "serves X's beans" link is a much-later idea, not now. Cafes enter discovery only as candidates admitted when they sell coffee online.
- 2026-11-07 (owner, refined): the registry is actual roasters only. Cafes and resellers are out; revisit cafes as a future item. Widening discovery to coffee_shop/cafe tags only serves to find mis-tagged roasters (e.g. Rival Bros).
- 2026-11-07 (owner rule): ask before any large spend (many subagents, mass labeling or site visits); say the cost up front.
- 2026-11-07 (owner, final): crawl roasters only; no cafes crawled. Discovery records the full source list (~47k Overture places) without fetching; the admin shows it and the owner flags roasters by hand; only flagged places (plus today's registry) are probed and crawled. No screen gate. #130 closed (its measurement kept as a record).
- 2026-11-08 (owner): review standards are not optional. No "quick pass": each review must show every skill run, its findings, the changes, and before/after screenshots the orchestrator has looked at.
- 2026-11-08 (owner rule): every design change: the orchestrator reviews the screenshots against the public site's clean design and sends them to the owner before calling it done.
- 2026-11-09 (owner): cutover2 / Cloud Shell steps tonight or tomorrow. Two weekly runs on the current pipeline before the crawl fixes; then merge the admin stack (#124 -> #125 -> #128 -> #129 -> #131 -> #135) and move to the new admin panel. All stack PRs reviewed and green; #135 CI pending at 3d6fa9b.

## 2026-11-11 decisions
- #136 (bag weights) and #138 (rule test gaps) merged; CI, deploy and the first eval-floors run on main green.
- Bag counts in words (#136 review M1): approved. Full CLAUDE.md process; builder stops before labeling to quote the count.
- Weekly private importer snapshots (Cafe Imports, InterAmerican): approved. Private HF dataset created by code.
- Site catalog counts must be computed from the release, never hard-coded.
- Producer/farm/lot extraction: yes, once the crawlers are updated (after cutover2).
- Royal: the owner questioned whether its terms bind a public, no-login page. Not crawled until decided.
- Catalog location (state column/filter, city, metro): feasibility study running.

## 2026-11-12 review rule (owner)
- A final review that requests changes goes back to that reviewer before merge. Any change made after the last review is reviewed too. Green CI alone is never enough.
- #145 broke this: it was merged without re-review. A post-merge review is running before Royal's first scheduled run (Wednesday 11-18, 17:37 UTC).
- No changes to shared crawler code without the owner's approval. Royal is "just another crawler": robots.txt is handled exactly as for every roaster.
- Importer derivation belongs in dbt, in a private target (Option A).

## 2026-11-12 owner decisions (cont.)
- #144: merge now; the Snowflake migrate (weekly, snowflake_migrate) runs after the 11-17 load. #148: merged as a private view; its evaluation is owed before any use.
- #139: build the real 95% gate (a fresh ~500-coffee sealed population sample on published prices), plus a census stratum of the coffees #139 changes.
- Labelers: A and B on Haiku; the tiebreaker C on Sonnet.
- AI may read and label the Royal text already stored privately: it's our scraped data. Only fetching Royal's site stays off-limits to agents.
- Investigate the WooCommerce enrichment-abstain jump (11-09).
- Weekly importer-build: in GitHub Actions after each snapshot, writing only to the private dataset.
- The private importers dataset was created on 2026-11-12 with the session token (it verified create and write access).

## 2026-11-12 owner: no new PRs, finish the open ones
Open PRs to finish: #139, #146, #149 (admin stack, after cutover2), #150, #151.
Backlog, not started until the owner says so:
- pin roaster names so discovery can't overwrite them (Dutch Bros "Hebron");
- #144 leftovers: MCP ISO names, stale counts in docs, stray punctuation;
- Claude enrichment queue ordering (prioritise current listed products);
- price-sort tail junk / non-coffee leak cleanup;
- missing regions (Popayán, Pitalito, Limmu…).
- Leftovers parked by the "finish, don't widen" rule:
  - #139: C1 (the census gate should also require accuracy at least main's), C2 ("1,500 bags" count parsing).
  - #151: iOS zoom on the 13 px Sort select, a truncated Filters label with a sort on, and the nav's `row-gap` having no effect.
  - #150: no test for the WAL cleanup, and the coverage baseline drops releases by "overall = 0".
- Owner rule (2026-11-13): dbt must stay idempotent and ingestion immutable. Same raw in, same output out, every
  build; stored raw is never rewritten or deleted; a job re-run must not change what an earlier run stored. Every
  builder and reviewer brief carries it, and it goes into docs/dbt/CONVENTION.md (#dbt-tests-docs).
- Health review (2026-11-12): OpenSSF Scorecard about 6.8/10, engineering B+, dbt "partly" one design. Owner said
  start all of it with independent agents: #153 supply-chain pins and provenance (ingest image included), #154 build
  exit code (#83), dbt-tests-docs, crawl-slo (#96), chain-tuesday-jobs (#84), then contracts on served tables, then
  the product dimension (design in the session scratchpad; defaults: dim_coffee stays coffee-only beside dim_product,
  the false "removed" event is a later measured change, admin drops become flags).
- Owner decisions (2026-11-13): out of the build path. CI builds the ingest image from main and attests it (#153); the
  by-hand Cloud Build route stays only as the runbook fallback. Terraform moves into GitHub too: plan posted on the PR,
  apply waits for the owner's Approve click in a protected environment. Needs the ruleset on main first; design in the
  session scratchpad (design-tf-ci), its own high-review PRs after cutover 2.
- Terraform in CI (owner, 2026-11-13): the owner made a second account, `jtouleyrou`, as the approver, and says the
  `terraform-apply` environment exists (not visible from the session: environments return 403). Its settings were
  confirmed by the owner 2026-11-13 (screenshots): reviewer jtouleyrou, prevent self-review, main only, admin bypass
  off. main still has no
  ruleset (owner to create, or says "create it"). Build order after #153 and #154: checked-in tfvars and guard scripts;
  plan-on-PR workflow; bootstrap terraform (owner applies once after cutover 2); apply workflow gated on the environment.
  Why a second account: this session acts as jtouley with admin, so jtouley-as-approver is no gate.
- Immutability audit (2026-11-13, scratchpad audit-immut/FINDINGS.md): published lake append-only; staging between
  jobs is not (compaction and merge delete or rewrite). Fixes touch shared crawler code: WAITING on the owner's "go".
- 2026-11-13: #154 and #153 merged; GitHub now builds and signs the ingest image (deploy 724a3bb green). Owner chose
  B: no cutover now; one combined last shell run once terraform-in-CI exists (tf-plan-ci, tf-apply-ci, building).
  Until that run: #154's build fix, the registry keep rule and the image pin are NOT live. Deadline: the jobs' image
  dates from about 2026-11-01..08, so it can be deleted after ~2026-12-01 once five newer ingest images exist; the
  combined run must happen well before then. First importers run dispatched from GitHub 2026-11-13 (importer=all).
## Plan from 2026-11-13 (owner agreed, after an outside product review)
The engineering is ahead of the evidence that people want it. Order of work:
1. Finish what is in flight (reliability and security): #163 then the owner's combined shell run, #156, #161, #164.
2. Data correctness: #87 (WooCommerce variant prices go null while available), #90 (OSM values reaching public_roaster).
3. Usage analytics on the website and app, fully anonymous (owner, 2026-11-13): which roasters and coffees are
   viewed, what is typed into the search bar, saves, shot logs, repeat visits. No IPs, no cookies or user ids in the
   events; search text scrubbed of anything that looks personal; stored privately like shot data, never published
   raw; aggregates only. Design first (how to count repeat visits without identifying anyone), then build.
4. Recommendation experiment ("Your Next Bag"): from five ratings per user, content-based picks vs most-popular vs
   random, scored on each user's later ratings (held-out by time, per CLAUDE.md). Needs about 20 users: owner to
   find them.
5. Paused until 4 has a result: admin stack growth beyond what is open (#124-#149 need a measured saving), Canada
   and Mexico (#70), new integrations, the product-dimension rebuild, the staging immutability fixes unless data
   loss shows up.
Also: benchmark coffee count and freshness against RoastDB (9,976 coffees, 582 roasters; we track 2,545 roasters).

- Owner decision (2026-11-13, option A): ship terraform apply in GitHub (#163) with the residual risk written down.
  Residual: Google issues the apply token to any job in the `terraform-apply` environment; it can't see who approved.
  An admin (this session acts as jtouley, admin) could loosen the environment's reviewers; today only the session
  proxy blocks that (403 on environments and rulesets). The workflow refuses if it can't verify a fresh jtouleyrou
  approval for this attempt. Proper fix later: the agent stops using an admin credential.
- Royal derivation: fixed in #152 (owner asked, 2026-11-12). Reviewed, CI green, held as draft for the held-out score:
  per CLAUDE.md it waits for the first scheduled week after 11-18 (week 11-09 was read while building). Then score main
  and #152 on a hash split grouped by lot, two labelers plus a tiebreak, Wilson intervals; merge if not worse.
- Real importer values in older test fixtures and unit tests on main (from #140/#148): owner decided 2026-11-12 to
  leave them (public on the importers' sites, a few lots, not the dataset). New tests use invented values only.
