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
