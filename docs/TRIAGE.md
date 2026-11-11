# Triage: eval and production-health alerts

Owner: jtouley. Last checked against `main` 0879254 on 2026-11-11.

Alerts arrive as GitHub issues labelled `triage`, one issue per alert key. The alert job updates an issue in place
while the alert keeps firing, comments only when what fails changes, reopens it if it fires again after closing,
and closes it once the alert recovers. GitHub's notification settings for the repository decide who gets an email.
Nothing here blocks a build, a load or a serve. The release gate stays the dbt tests in `coffeedata build`
(docs/ingestion/RUNBOOK.md §5.1).

## Quick checklist

- [ ] 1. Read the issue's table: which eval or rule, which set or platform, the measured value against the bound.
- [ ] 2. Open the linked run, and check whether the commit or the release named there is still the latest.
- [ ] 3. Use the section below for the issue's label (`eval-regression` or `prod-health`) and key.
- [ ] 4. Critical `prod-health`: decide on a rollback today ([When to roll back](#when-to-roll-back)).
- [ ] 5. Fix the cause. The next run closes the issue. Don't close it by hand while the check still fails.

## Where alerts come from

| Key | Raised by | When | Severity |
|---|---|---|---|
| `eval:<eval>` (`eval:origin`, `eval:bag_grams`, …) | `ci.yml` job `alert` (push to main); `eval-floors.yml` job `alert` (push to main, Tuesdays 19:23 UTC, by hand) | an accuracy floor or ceiling in `tests/test_<eval>_accuracy.py` fails, or the eval errors before scoring | critical |
| `pipeline:eval-floors` | `eval-floors.yml` | the run wrote no floor report (no Hub token, a failed download, a changed checksum) | warning |
| `prod:<rule>` | `prod-health.yml` (Tuesdays 11:43 UTC, by hand) | a metric of the served release crosses its bound in `config/prod_health.toml` | warning or critical, per bound |
| `pipeline:prod-health` | `prod-health.yml` | the served release couldn't be measured | warning |

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
in `config/prod_health.toml` with its baseline, its history and its bounds.

| Rule (issue key) | Metrics | A breach usually means |
|---|---|---|
| `origin`, `process`, `roast_level` | `fill:<rule>[:<platform>]`: the share filled in; `shift:changed:<rule>`: the share of coffees in both releases whose value changed | an extraction rules regression, enrichment not run or capped, or a platform's descriptions no longer read |
| `grams` | `fill:grams[:<platform>]` (a bag weight read); `shift:changed:grams`; `shift:median:grams` | a bag_weight change (#136, #139), or a platform's variant titles changed shape |
| `price_per_100g` | `fill:price_per_100g[:<platform>]`; `shift:changed:price_per_100g`; `shift:median:price_per_100g` | a lost weight or price, a currency or unit error, or a price wipe on one platform |
| `classifier` | `fill:classifier`: the share decided by Claude's verdict, judged on the build's own `min_verdict_share` 0.85 | verdicts missing or late, or no verdict model. Starts once a release carries `classified_by` |
| `enrichment` | `abstain:enrichment[:<platform>]`: the share the rules left to Claude (high is bad) | the extraction rules abstaining more, so Claude's spend and backlog grow |
| `volume` | `rows:listed[:<platform>]`; `shift:rows_ratio:listed[:<platform>]`: listed coffees against the previous run; `shift:retained`: the previous run's coffees still listed | a partial crawl, a platform outage, a classifier swing, or a store's archive appearing |
| `fetch` | `build:run_ok_share`, `build:run_catalog_ok_share`, `build:adapter_ok_share:<platform>` (mart_build_profile, reused) | fetches failing. fetch-health (RUNBOOK.md §8) should have said so too |
| `build-tests` | `build:test:<test>`: failures of the dbt band tests that only warn (`fill_and_freshness_in_band`, `build_metrics_in_band`, `adapter_volume_in_band`, `coverage_claims_match_build`, `verdicts_cover_catalog`); `build:passed` | the build's own warnings fired. Read them in `coffeedata build-diff` |
| `freshness` | `freshness:served_age_days` (warning above 8, critical above 15) | a missed week: no build passed, or nothing was served |

First checks:

1. Read the issue's table: the value, the bound and the baseline. One platform or all? One rule or several? Several
   rules on one platform point at the crawl, and one rule on every platform points at the code.
2. Compare the served release with the previous one: `uv run coffeedata build-diff` (RUNBOOK.md §5.1 a). It
   only reads the lake.
3. Check what changed on main since the previous release, for the rule's code (`extract.py`, `bag_weight.py`,
   `classify.py`, `verdict.py`, the dbt models), and the week's crawl (fetch-health, `job_manifests`).
4. The metrics and the full judgement are in the run's summary and its `prod-health` artifact (`metrics.json`,
   `alerts.json`). To re-run on a local, read-only copy of the lake:
   ```sh
   uv run python -m coffeedata.prod_health fetch "$LAKE_URL" /tmp/health-lake   # read-only download
   uv run python -m coffeedata.prod_health measure /tmp/health-lake /tmp/metrics.json
   uv run python -m coffeedata.prod_health judge /tmp/metrics.json
   ```
5. **A breach that is right** (a real market change, a planned rule change that lowers a fill rate on purpose) is
   handled by regenerating the bounds from a longer history ([Thresholds](#thresholds)) in a PR that says why.
   Don't edit one bound to silence an issue.

**`pipeline:prod-health`**: check the `LAKE_URL` variable and the Hub token, and that `marts/release.parquet`
names a release whose `dim_coffee` is in the lake (RUNBOOK.md §5).

## When to roll back

Roll back the served release (`gh workflow run weekly -f rollback_to=previous`, RUNBOOK.md §5) when **all** of
these hold:

- the issue is **critical** and comes from `prod-health`;
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

`config/prod_health.toml` is the single versioned list (`version`, `revision`). It is generated by
`python -m coffeedata.prod_health baseline` from the release history in the lake, and its header records which
releases were used. Each bound starts at the worst value seen and is widened by whichever is larger:
- k × 1.4826 × MAD (k = 3 for a warning, 6 for critical);
- a minimum margin:
  - shares: 0.05 for a warning (as `band_min_share_halfwidth`) and 0.15 for critical;
  - volume ratios and median shifts: 0.10 and 0.25.

Fill and retention shares are judged on the low side, abstain and changed shares on the high side, and volume and
medians on both sides.

The first baselines (revision 2026-11-11) come from the four served runs the lake holds:
- `marts/` at lake commits `c3f7595` (2026-10-27) and `41b68c6` (2026-10-28);
- releases `20261104T041920-5060ad` (run 2026-11-02) and `20261110T061836-971bfe` (run 2026-11-09).

That gives three week-over-week pairs. The first pair predates Claude's verdicts, so the volume bounds are loose
(Shopify's listed count fell 17% then). Regenerate the file once 8–12 weekly releases exist, as `band_k` is
recalibrated (dbt_project.yml), and bump `revision`. The tests check that no bound fires on its own history:
`tests/test_prod_health.py::test_the_checked_in_thresholds`.

The eval floors and ceilings stay where they are, in each `tests/test_*_accuracy.py`. The alerts read them from
the test run's report (`EVAL_REPORT`, `tests/evalfloor.py`) and never copy them.

## Permissions and safety

- The alert jobs use `GITHUB_TOKEN` with `issues: write` and `contents: read` only, install nothing, and run main's
  `coffeedata/triage.py`, which uses only the standard library. They run on pushes to main and on schedules, never
  on a pull request, so no PR code runs with the token.
- The Hub token stays on its one download step, in jobs that only run on main (eval-floors' `floors`,
  prod-health's `measure`), and those jobs have no `issues: write`.
- The Cloud Run jobs get no GitHub token, and no GCP resource was added. prod-health reads the lake read-only and
  never writes to it.
- To test without touching GitHub: `python -m coffeedata.triage evals <report.json> --workflow ci --dry-run`
  prints the issue it would open. `tests/test_triage.py` and `tests/test_prod_health.py` cover deduplication,
  updates, reopening and recovery against an in-memory GitHub.
