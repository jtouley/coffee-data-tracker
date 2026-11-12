# Triage: eval and production-health alerts

Owner: jtouley. Last checked against `main` 0879254 on 2026-11-11.

Alerts arrive as GitHub issues labelled `triage`, one issue per alert key, opened by the workflows' bot. The alert
job updates an issue in place while the alert keeps firing, comments only when what fails changes, reopens it if it
fires again after closing, and closes it once every check it failed on is measured again and passes. It rewrites
the issue's description on every run, so **add notes as comments**. GitHub's notification settings for the
repository decide who gets an email.

Nothing here blocks a build, a load, a serve or a deploy. ci's alerts run in their own workflow, after ci, so an
Issues API outage can't turn ci red. The release gate stays the dbt tests in `coffeedata build`
(docs/ingestion/RUNBOOK.md §5.1).

## Quick checklist

- [ ] 1. Read the issue's table: which eval or rule, which set or platform, which bound was crossed.
- [ ] 2. Open the linked run, and check whether the commit or the release named there is still the latest.
- [ ] 3. **Was it intended?** Read the PRs merged since the previous release (their Effect sections) and the
      CHANGELOG ([Was it intended?](#was-it-intended)).
- [ ] 4. Use the section below for the issue's label (`eval-regression` or `prod-health`) and key.
- [ ] 5. Critical `prod-health` that no change explains: decide on a rollback today
      ([When to roll back](#when-to-roll-back)).
- [ ] 6. Fix the cause. The next run that measures it closes the issue: every check it failed on passes again, or
      (for a floor that crashed, or one read from its message such as bag_grams) every test of that set passes.
      Don't close it by hand while the check still fails.

## Where alerts come from

| Key | Raised by | When | Severity |
|---|---|---|---|
| `eval:<eval>`, in-git floors (`classifier`, `sealed`, `roaster_dedup`, `bag_grams`) | `eval-alerts.yml`, after ci completes on a push to main, from that ci run's report | an accuracy floor or ceiling in `tests/test_<eval>_accuracy.py` fails, or the floor test fails without a failing check (it crashed) | critical |
| `eval:<eval>`, private floors (`wholesale`, `origin`, `match`, `dq`) | `eval-floors.yml` job `alert` (push to main, Tuesdays 19:23 UTC, by hand) | the same, for the `private_eval` floors | critical |
| `pipeline:eval-floors` | `eval-floors.yml` | the run scored no private floor (no report, an empty one, or every floor skipped: no Hub token, a failed download, a changed checksum, a collection error) | warning |
| `prod:<rule>` | `prod-health.yml` (Tuesdays 11:43 UTC, by hand) | a metric of the served release crosses its bound in `config/prod_health.toml` | warning or critical, per bound |
| `pipeline:prod-health` | `prod-health.yml` | the served release couldn't be measured | warning |

Each workflow judges only the evals it owns: ci never touches a private eval's issue (its private floors skip), and
eval-floors never touches an in-git one. Only the floor files (`tests/test_<eval>_accuracy.py`) are reported, so a
unit test that calls the `floors` helper is never taken for an eval.

On a pull request nothing is opened. A failed floor fails ci's `test` job, and both its message and its `::error`
annotation name the eval, the set, the measured value and the floor (with the Wilson 95% interval for a count), e.g.
`eval origin, set train: weighted agreement 95.10% < floor 95.90%`.

The existing email alerts are unchanged (`src/terraform/alerts.tf`, RUNBOOK.md §6): a failed or timed-out Cloud
Run job, a Scheduler entry that didn't start, and `site.yml`'s red Tuesday when the served release isn't this week's.
These issues cover what those emails can't see: a build that passes, with data that got worse.

## eval-regression: a floor failed on main

The floors are the labeled evals of docs/RULES.md. A failure on main means the merged code, the lockfile, or an
eval's labels or inputs no longer reach a bound the PR passed, or that never ran on the PR (the private floors run
only in eval-floors).

1. Read the failing row. A floor (`>=`) means fewer right answers. A ceiling (`<=`) means more confidently wrong
   ones, which CLAUDE.md treats as the worse of the two.
2. Find the change: `git log <last green sha>..<this sha> -- src/ eval/ tests/ uv.lock`. The last green run of the
   same workflow is in the Actions tab.
3. Reproduce:
   - in-git floors: `uv run pytest -q tests/test_<eval>_accuracy.py`;
   - private floors: build the inputs first (`uv run python eval/floor_inputs.py <dir>` with a read token, then
     set `ORIGIN_EVAL_DIR` and the other variables it prints).
   Only totals print for validation. Read train examples only (CLAUDE.md steps 3–4).
4. Fix forward or revert the change. **Never lower a floor or raise a ceiling to make it pass** (CLAUDE.md step 8).
   A floor moves only up, when a measured change earns it.
5. An `error` row means the eval couldn't score at all: an input checksum, a dbt compile or a redrawn sample that
   isn't the labelled one. Treat it like `pipeline:eval-floors`.

**`pipeline:eval-floors`**: the floors weren't scored. Check the token (`HF_READ_TOKEN`, else `HF_TOKEN`) and the
pinned lake revisions in `eval/floor_inputs.py` (RULES.md, Private inputs). Then run the workflow by hand.

## prod-health: the served release looks wrong

Production has no labels, so each rule is watched through proxies of the served release's `dim_coffee`. The
population is listed retail coffees: `is_listed` and not wholesale, as the site counts them. Each metric is listed
in `config/prod_health.toml` with its baseline, its history, its bounds and any `flagged` history values.

The issue, the run's summary and its log name each metric, its status and the bound it crossed, never the measured
value. The lake is private and this repository is public. Get the values locally (step 4).

| Rule (issue key) | Metrics | A breach usually means |
|---|---|---|
| `origin`, `process`, `roast_level` | `fill:<rule>[:<platform>]`: the share filled in; `shift:changed:<rule>`: the share of coffees in both releases whose value changed | an extraction rules regression, enrichment not run or capped, or a platform's descriptions no longer read |
| `grams` | `fill:grams[:<platform>]` (a bag weight read); `shift:changed:grams`; `shift:median:grams` | a bag_weight change (#136, #139), or a platform's variant titles changed shape |
| `price_per_100g` | `fill:price_per_100g[:<platform>]`; `shift:changed:price_per_100g`; `shift:median:price_per_100g` | a lost weight or price, a currency or unit error, or a price wipe on one platform |
| `classifier` | `fill:classifier`: the share decided by Claude's verdict, judged on the build's own `min_verdict_share` 0.85 | verdicts missing or late, or no verdict model. Starts once a release carries `classified_by` |
| `enrichment` | `abstain:enrichment[:<platform>]`: the share the rules left to Claude (high is bad) | the extraction rules abstaining more, so Claude's spend and backlog grow |
| `volume` | `rows:listed[:<platform>]`; `shift:rows_ratio:listed[:<platform>]`: listed coffees against the previous run; `shift:retained`: the previous run's coffees still listed | a partial crawl, a platform outage, a classifier swing, or a store's archive appearing |
| `fetch` | `build:run_ok_share`, `build:run_catalog_ok_share`, `build:adapter_ok_share:<platform>` (mart_build_profile, reused) | fetches failing. fetch-health (RUNBOOK.md §8) should have said so too |
| `build-tests` | `build:test:<test>`: failures of the dbt band tests that only warn (`fill_and_freshness_in_band`, `build_metrics_in_band`, `adapter_volume_in_band`, `coverage_claims_match_build`, `verdicts_cover_catalog`); `build:passed` | the build's own warnings fired. Read them in `coffeedata build-diff`. The served builds so far (to run 2026-11-09) record only `build_metrics_in_band`: their build image predates the other four, which show as "not measured" in the summary until a build runs them |
| `freshness` | `freshness:served_age_days`: days from the served release's run (a Monday) to the check (Tuesday) | warning above 7 (one missed week), critical above 14 (two). No build passed, or nothing was served |

First checks:

1. Read the issue's table: the metrics and the bounds they crossed. One platform or all? One rule or several?
   Several rules on one platform point at the crawl, and one rule on every platform points at the code.
2. **Was it intended?** See below. A merged change that explains the move gets an entry in
   `config/prod_health_expected.toml`, not a rollback.
3. Compare the served release with the previous one: `uv run coffeedata build-diff` (RUNBOOK.md §5.1 a). It
   only reads the lake. Check the week's crawl too (fetch-health, `job_manifests`).
4. The statuses are in the run's summary and its `prod-health` artifact (`alerts.json`). For the values, re-run on
   a local, read-only copy of the lake:
   ```sh
   uv run python -m coffeedata.prod_health fetch "$LAKE_URL" /tmp/health-lake   # read-only download
   uv run python -m coffeedata.prod_health measure /tmp/health-lake /tmp/metrics.json
   uv run python -m coffeedata.prod_health judge /tmp/metrics.json
   ```
5. **A breach that is right but no PR explains** (a real market change) is handled by regenerating the bounds from
   a longer history ([Thresholds](#thresholds)) in a PR that says why. Don't edit one bound to silence an issue.

### Was it intended?

Before treating a breach as a regression, check whether a merged change meant it:

1. List the PRs merged since the previous release:
   `gh pr list --state merged --search "merged:>=<previous release's run date>"`.
   Read each one's *Effect* section, and the CHANGELOG's Unreleased entries. Changes to `extract.py`,
   `bag_weight.py`, `classify.py`, `verdict.py` or the dbt models matter most.
2. If one explains the metric, the direction and roughly the size, add an entry to
   `config/prod_health_expected.toml` in a PR. Each entry needs the metric (a glob), `change` (down, up or
   either), `by` (the most it may move from its baseline), the release window (`from_run`, `until_run`), the PR
   and why.
   - A matching breach is then reported as "expected (#PR)" in the run summary, and the issue closes on the next
     run.
   - A bigger move, or one outside the window, still alerts.
3. A lasting level shift (a fill rate that stays lower) ends when the bounds are regenerated with the new release
   in the history, before the entry's `until_run`.
4. The entries for #136 are already there. Its first release (run 2026-11-16) drops Shopify grams coverage by about
   6.6 points and changes about 22% of bag sizes and 15% of prices per 100 g. That is reported, not paged.

**`pipeline:prod-health`**: check the `LAKE_URL` variable and the Hub token, and that `marts/release.parquet`
names a release whose `dim_coffee` is in the lake (RUNBOOK.md §5).

## When to roll back

Roll back the served release (`gh workflow run weekly -f rollback_to=previous`, RUNBOOK.md §5) when **all** of
these hold:

- the issue is **critical** and comes from `prod-health`;
- no merged change explains it ([Was it intended?](#was-it-intended)): rolling back an approved change undoes it;
- the breach is in what customers read: a fill rate, `price_per_100g`, or volume, not a build-record metric;
- `build-diff` confirms it against the previous release;
- the previous release doesn't have the same problem.

Don't roll back for:

- a warning: fix it before next Tuesday;
- an `eval-regression`: the served data was built by the code that was running then, so revert the code instead;
- a freshness alert: rolling back makes the data older. Fix the build (RUNBOOK.md §5.1).

After a rollback, the next prod-health run measures the release that is served then. Run it by hand
(`gh workflow run prod-health`) to confirm the issue closes.

## Thresholds

`config/prod_health.toml` is the single versioned list (`version` 2, `revision`). It is generated by
`python -m coffeedata.prod_health baseline` from the release history in the lake, and its header records which
releases were used. Each bound is centred on the history's **median**, never on the worst value seen. A margin goes
around it:

- **Fill, retention and fetch shares** (low side). The margin is the largest of three terms, capped at 25% of the
  median for a warning and 45% for critical:
  - a relative drop: 3% of the median for a warning, 10% for critical;
  - 4 or 8 binomial standard errors at the platform's listed count `n`;
  - 3 or 6 robust standard deviations of the history (1.4826 × MAD).

  So a 3-point drop on Shopify's 96% grams fill warns. A halving on a low-fill platform is always critical.
  Ordinary sampling noise doesn't page.
- **Abstain and changed shares** (high side): the median plus at least 0.02 for a warning and 0.05 for critical.
- **Volume ratios**: 1 ± at least 0.10 / 0.25. **Median shifts**: 0 ± at least 0.05 / 0.15.
- **Fixed, with no history**: a warn-only band test warns on any failure, and the classifier is judged on
  `min_verdict_share` 0.85. Release age warns above 7 days and is critical above 14.

**Flagged history.** A history value outside its own warning band is not used to widen the band. It is listed as
`flagged` in the config, as a possible regression to look into. At revision 2026-11-11 two are flagged:
- `abstain:enrichment:woocommerce` went from 0.013 to 0.196 in run 2026-11-09. It breaches as served: the first
  prod-health run will open `prod:enrichment`, and that is the first thing to triage;
- `shift:rows_ratio:listed:generic` was 1.17 between the 10-27 and 10-28 marts.

**Simulated on the 11-09 release** (a copy, blanking Shopify grams):
- a 3-point drop warns, and so does a 4-point drop;
- a #136-sized drop (6.6 points) warns on `fill:grams:shopify`, `fill:grams` and `shift:changed:grams`;
- halving Square Online's grams is critical.

`tests/test_prod_health.py` pins these cases on the checked-in bounds.

The first baselines come from the four served runs the lake holds:
- `marts/` at lake commits `c3f7595` (2026-10-27) and `41b68c6` (2026-10-28);
- releases `20261104T041920-5060ad` (run 2026-11-02) and `20261110T061836-971bfe` (run 2026-11-09).

Regenerate the file once 8–12 weekly releases exist (as `band_k` is recalibrated, dbt_project.yml), and after a
change acknowledged in `config/prod_health_expected.toml` lands. Bump `revision` when you do.

The eval floors and ceilings stay where they are, in each `tests/test_*_accuracy.py`. The alerts read them from
the test run's report (`EVAL_REPORT`, `tests/evalfloor.py`) and never copy them.

## importers: the private importer build failed

**`pipeline:importer-build`** (label `importers`, warning), raised by the `alert` job of `importers.yml` after
the `build` job, which follows each importers run's snapshot on main once it has passed. It fires when the private dbt build
(`coffeedata importer-build --publish`) fails, or when it passes but its copy can't be sent to the dataset, and
closes on the next build that passes. The issue gives the build's status, the run and the commit: nothing from a lot.

- **What isn't affected.** The snapshot job finished before the build started, so every week it stored stays stored
  (raw files are never rewritten). The private dataset's `derived/importer_offers.parquet` stays the last build that
  passed: a failed build sends nothing.
- **Reading the run.** The repository and its logs are public, so the build step prints counts only: dbt's nodes by
  status (model, test, unit_test) and, when sent, the lots, lots per importer, weeks and latest week. dbt's own output
  went to a file on the runner, never printed or uploaded, since a failing query's error can quote a lot's value.
- **Reproduce** with read access to the private dataset:
  `COFFEEDATA_IMPORTERS=<its hf:// URL> uv run coffeedata importer-build` prints dbt's output on your own console.
  In the issue and its comments, give the failing model or test and the kind of error, never a value.
- **Recover.** Fix it on main. The next Wednesday's run builds again; to build sooner, dispatch
  `gh workflow run importers` (a dispatch never fetches Royal, and stored weeks are skipped). Never re-run a
  scheduled run for Royal's sake: a re-run never fetches Royal either.
- **Token or privacy refusal.** `HF_TOKEN is not set`, `not private` or `holds files this job didn't write`: the same
  checks as the snapshot (`importers.ensure_private`), which the build passes before sending anything. The dataset may
  hold `raw/`, `README.md`, `.gitattributes` and, under `derived/`, only `importer_offers.parquet`.

To see the issue without touching GitHub: `PYTHONPATH=src python3 -m coffeedata.importer_build_alert failure
--dry-run`. `tests/test_importer_build_alert.py` covers opening, updating, closing and reopening against an
in-memory GitHub.

## Permissions and safety

- The alert jobs use `GITHUB_TOKEN` with `issues: write` and `contents: read` only, install nothing, and run main's
  `coffeedata/triage.py`, which uses only the standard library. eval-alerts' job also has `actions: read`, to
  download that one ci run's report.
- eval-alerts skips a ci run that isn't on main's tip. If the tip's own ci fails before its tests (lint), the
  floors' alert waits for the next commit that reaches them, so it is delayed, never lost. This is left as is.
- They never run on a pull request, so no PR code runs with the token. eval-alerts runs on `workflow_run`, which
  always uses main's workflow and code. It acts only for ci's push runs on main in this repository, downloads only
  that run's artifact (by its id), and alerts only when that run is on main's current tip.
- Issue lookup trusts only issues the workflows' bot opened (`TRIAGE_AUTHOR`, default `github-actions[bot]`) that
  carry the key marker. A hand-made or copied issue never takes over a key.
- **Private data.** The lake is private and this repository public. Production issues, the run summary, the log and
  the `prod-health` artifact carry metric names, statuses and bounds, never a measured value.
  - The bounds and their history, in `config/prod_health.toml`, are catalog-wide and per-platform aggregate shares
    and counts. That is the same kind of coverage figure the site and the listing already publish.
  - Keeping them in the public repository is the owner's decision. Delete `history` and `flagged` from the file if
    they should stay private.
  - Eval issues show the floors' scores, which `docs/RULES.md` already publishes.
- The Hub token stays on its one download step, in jobs that only run on main (eval-floors' `floors`,
  prod-health's `measure`), and those jobs have no `issues: write`.
- The Cloud Run jobs get no GitHub token, and no GCP resource was added. prod-health reads the lake read-only and
  never writes to it.
- To test without touching GitHub: `python -m coffeedata.triage evals <report.json> --workflow ci --dry-run`
  prints the issue it would open. `tests/test_triage.py` and `tests/test_prod_health.py` cover deduplication,
  updates, reopening and recovery against an in-memory GitHub.
