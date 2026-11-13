# Triage: eval, production-health and importer alerts

Owner: jtouley. Last checked against `main` 060cd16 on 2026-11-12.

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
- [ ] 4. Use the section below for the issue's label (`eval-regression`, `prod-health` or `importers`) and key.
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
| `importers:weekly` | `importers-alerts.yml`, after every `importers` run on main and Thursdays 09:23 UTC (by hand too) | an importer has no stored week for the due week: its run failed, the scheduled run never started, or the week is stale | warning |
| `pipeline:importer-build` | `importers.yml` job `alert`, after the `build` job that follows each snapshot on main | the private importer build (`coffeedata importer-build --publish`) failed, or its copy couldn't be sent; the stored weeks aren't affected | warning |
| `pipeline:importers-health` | `importers-alerts.yml` | the check couldn't judge the week: no Hub token, a failed dataset or runs listing, or (once a week is due) a dataset the Hub calls "not found" | warning |

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
| `enrichment` | `abstain:enrichment[:<platform>]`: the share of listed coffees the rules leave incompletely described (`dim_coffee.rules_complete` false: no origin and not a blend, no process, or no tasting notes), whether or not Claude has reached them (high is bad). `coverage:enrichment[:<platform>]`: the share of those that have a Claude row, i.e. how far the 2,000-a-week enrichment backlog has got (low is bad; only a drop alerts) | abstain up: the extraction rules abstaining more, so Claude's spend and backlog grow. Coverage down: Claude's rows lost or not joined (a wiped enrichments table, a key change). A stalled enrichment job keeps its rows, so coverage stays flat or sinks slowly and may not alert |
| `volume` | `rows:listed[:<platform>]`; `shift:rows_ratio:listed[:<platform>]`: listed coffees against the previous run; `shift:retained`: the previous run's coffees still listed | a partial crawl, a platform outage, a classifier swing, or a store's archive appearing |
| `fetch` | `build:run_ok_share`, `build:run_catalog_ok_share`, `build:adapter_ok_share:<platform>` (mart_build_profile, reused) | fetches failing. fetch-health (RUNBOOK.md §8) should have said so too |
| `crawl-slo` | `build:crawl_slo_met` (1: the run fetched more than 99% of the known roasters), `build:crawl_slo_burn_rate`, `build:crawl_slo_budget_remaining`; `build:crawl_slo_met:<platform>` only reports (mart_crawl_slo_profile, recorded by the build) | the crawl missed its SLO. Warnings only, and never a reason to roll back: see [The crawl success SLO](#the-crawl-success-slo) |
| `build-tests` | `build:test:<test>`: failures of the dbt band tests that only warn (`fill_and_freshness_in_band`, `build_metrics_in_band`, `adapter_volume_in_band`, `coverage_claims_match_build`, `verdicts_cover_catalog`, `origin_countries_are_mapped`: an origin spelling the ISO seed doesn't have, published with no country until it is labelled and added, eval/origin_iso); `build:passed` | the build's own warnings fired. Read them in `coffeedata build-diff`. The served builds so far (to run 2026-11-09) record only `build_metrics_in_band`: their build image predates the other four, which show as "not measured" in the summary until a build runs them |
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

## importers: a weekly snapshot is missing

The importers workflow (`importers.yml`) stores one private file a week per importer
(`raw/week=<Monday>/importer=<key>.parquet`) from its Wednesday 17:37 UTC scheduled run. `importers-alerts.yml`
judges the dataset, not the run: after every importers run on main, and each Thursday morning in case no run
happened, it lists the dataset's file names (never a file) and keeps one `importers:weekly` issue open while any
importer lacks the due week.

**Objective.** Every importer's week is stored by the Thursday check: 3 of 3 (importer, week) pairs a week. A missed
`cafe_imports` or `interamerican` week can be fetched again the same week. A missed `royal_coffee` week can't: only a
scheduled first attempt may fetch it, so it is lost, and the next chance is the following Wednesday. That makes
detection the gate that matters, and why the check also runs when no importers run happened.

**The due week.** This week's Monday once its scheduled run has completed, or 4 hours after 17:37 UTC on Wednesday
(the job's 2-hour timeout and room for a late start), whichever comes first; until then, last week's. Weeks before
2026-11-16, the first scheduled run's, are never due.

**What the issue shows.** One row per importer: its key, `ok` or `missing <n> week(s)` (weeks missed in a row) and
its last stored week (a Monday), the due week, and the scheduled run that should have stored it (its conclusion and
link, or that none started). Nothing from a lot, and no store address. A new week missed changes the statuses, so it
adds a comment; the same state at the next check doesn't.

Quick checklist:

1. Read the summary. Which of the three is it?
   - **No scheduled run started.** Check that `importers` is enabled in the Actions tab: someone may have disabled
     it by hand. GitHub can also drop or delay a scheduled event under load. Since `queue: max`, a dispatch no
     longer cancels a pending scheduled run.
   - **The run failed.** Open the linked run and read the `importer-snapshot` step's line per importer, its status and
     error. In comments here, give the importer and its status only: an error can name a store's address. A run
     also ends `failure` when only its `build` job failed. That stores every week, so it opens
     `pipeline:importer-build` instead of this issue; this issue lists only the weeks the dataset lacks.
   - **The run succeeded but a week is missing.** Royal was skipped (the address went only to a scheduled first
     attempt: a re-run, or a dispatch, skips it), or the run stored the week after this check ran (the next check
     closes the issue).
2. Fix the cause. The importer's line names it: a fetch failure, a changed layout, prices in a cell, a thinned
   listing (under half the last stored week) or `DeadlineReached`.
3. Recover:
   - `cafe_imports`, `interamerican`: dispatch the same week, `gh workflow run importers -f importer=<key>`. For a
     real drop, checked by hand, add `-f accept_drop=true`.
   - `royal_coffee`: **never dispatch or re-run for it.** No AI agent may access its site. The week is lost: fix the
     cause before next Wednesday. A real drop is accepted by the `ROYAL_ACCEPT_DROP_WEEK` repository variable, set
     to the Monday the failure names; the next scheduled run reads it.
4. Verify. The check that follows each importers run lists the dataset again and closes the issue once every
   importer has the due week. A fix that waits for next Wednesday closes it then. To check by hand without fetching
   anything: `gh workflow run importers-alerts`.
5. Escalate. Two weeks missed in a row for one importer (`missing 2 weeks`) means the fix didn't hold: look before
   the next Wednesday, not after.

The issue is never closed by hand while a week is still missing. The next check would reopen it.

**What this check can't see.** GitHub automatically disables the scheduled workflows of a public repository with
no activity for 60 days. That stops importers-alerts' Thursday schedule along with importers', and with no
importers run there's no `workflow_run` either, so no issue opens. In a repository this active it's unlikely. If
the repository goes quiet, re-enable both workflows in the Actions tab.

**`pipeline:importers-health`**: the check couldn't judge the week, so `importers:weekly` is left as it is. The
summary says which:
- **No Hub token**: neither `HF_READ_TOKEN` nor `HF_TOKEN` is set.
- **"Not found"**: `IMPORTERS_URL` names the wrong dataset, or the token can't read the private importers dataset.
  The Hub answers both the same way. Before the first due week (2026-11-16) this stays silent, since the dataset
  may not exist yet.
- **The dataset listing failed**: a Hub outage or a rejected token. Transient errors are retried first.
- **The runs listing failed**: the Actions API. The weeks step runs anyway (`if: ${{ !cancelled() }}`), so the
  weeks are still judged, by the time alone.

Check the token's read access and `IMPORTERS_URL` before anything else, then re-run `gh workflow run
importers-alerts`.

**Owner check before the first scheduled run.** After the first dispatch of `importers`, open the
`importers-health` artifact of the importers-alerts run that follows it. Confirm that `weeks.json` says
`"dataset": "found"`. That is the only proof, before 2026-11-18, that the check's token can read the dataset.

To try it without touching GitHub, put the check's two files in a folder and run
`PYTHONPATH=src python3 -m coffeedata.triage importers <folder> --dry-run --now 2026-11-19T09:23`. The files are the
`importers-health` artifact of an importers-alerts run (`weeks.json`, `runs.json`).

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

## The crawl success SLO

The owner's target (#96, 2026-11-02): **more than 99% of the known roasters are fetched ok in each weekly run**,
as one aggregate over every connector. It is a benchmark, not a gate. Nothing fails a build or holds back a
release on it; the gate's floors stay `run_ok_rate` (90% of the roasters with a catalog) and `adapter_ok_rate`.
A miss opens the `prod:crawl-slo` issue as a warning on Tuesday, and that is all it does.

**The indicator**, as dbt computes it (`int_crawl_slo_roasters`, then `mart_crawl_slo`; the edge cases are unit
tests in `dbt/models/crawl_slo.yml`). Each run is judged on what that run stored: its manifests and its fetches.

- **Which runs are scored.** The scheduled weekly runs only. The repo has no single flag for that, so two existing
  definitions are combined. The weekly run is dated the Monday of its week: the crawl jobs run on Mondays and date
  their run with `connectors.week_of`. And it set out to fetch every roaster: one of its manifests has mode `full`,
  the test `run_completeness` uses; a run from before manifests is taken as full. An ad-hoc ingest dated another
  day (`--run-date` defaults to today; the lake holds 2026-10-27, 10-28 and 10-30) and a Monday with only sample,
  `--only` or `retry_failed` manifests are not scored and spend no budget. A week whose crawl never started has no
  stored run either; `freshness:served_age_days` reports that.
- **Known roasters (the denominator).** Every roaster the run listed in a full manifest (for a run from before
  manifests, every roaster it tried), plus every roaster an earlier scored run listed that this one didn't. A
  partial pull (`--only`, "Pull now", sample, `retry_failed`, or an ad-hoc run) never makes a roaster known, and a
  roaster removed from the registry is not known in the runs that didn't list it (read as the registry is now, like
  an alias). The manifest is the crawl's pool when the run
  started: the roasters with a platform, not opted out and not an alias, which is the owner's "expected to fetch".
  The second part is the roasters the crawl has stopped trying (`unreachable` after three failed runs): they count
  as not fetched, so giving up on a store never raises the share.
- **Success (the numerator).** An attempt in the run was ok: `int_roaster_runs.ok`, the same `ok` as
  `run_completeness` and `run_ok_share`. A retry that fixed a failure counts. So do a partial read and an ok
  fetch of a store with no products.
- **Counted as not fetched:** a failed fetch; a store parked or rate-limited that no later attempt of the run
  read; one an edge challenged; one that crashed the crawler twice (quarantined); a store found closed,
  password-protected or disallowed that run (`unavailable`); a roaster the run never reached; a roaster the crawl
  has stopped trying. The gate's ok rates leave closed and quarantined stores and no-catalog roasters out; the SLO
  does not (the owner: "unreachable and no-catalog roasters count against the SLO").
- **Chains count** like any other roaster: they are crawled, and only left out of the public tables.
- **Not counted**, only among the roasters the run didn't list, with the reason in `excluded_reason`:
  - `opted_out`: it opted out before the run started (`stg_registry.opted_out_at`, the first registry row that
    says so). An opt-out in the seed list (`config/roasters.csv`) has no date and is taken as from the start;
  - `alias`: another roaster_id serves the same store, which is counted once. **The one attribute read as it is
    now**, not as it was at the run: raw records no date for it. It only removes a roaster the run didn't list,
    whose store the canonical roaster's row already counts.
  - A roaster no stored run has listed or tried yet has no row, whatever the registry says now.

Where the owner's notes don't decide, the stricter reading was taken: closed and quarantined stores, and
roasters the crawl gave up on, count against the SLO. Each can be loosened later; the outcome column
(`ok`, `failed`, `challenged`, `unavailable`, `quarantined`, `not_attempted`) shows how much of a miss each one is.

**The numbers** (`mart_crawl_slo`, one row per scored run and platform; `all` is the SLO):

- `sli` with its Wilson 95% interval (`sli_low`, `sli_high`), and `met` (above the target; exactly 99% is a miss);
- `burn_rate`: (1 − sli) / (1 − target). 1 is the rate the target allows. Above 4, one run has spent about the
  whole window's budget (exactly, when the window's runs have the same number of roasters);
- `budget_remaining`: the share of the window's error budget left. The window is **anchored on the run**, not on
  a date: that run and the scored runs before it, 4 in all (`crawl_slo_window_runs`; about 28 days of weekly
  runs), the same runs for every platform. The budget is the failures the target allows over the window's
  roasters, 1% of them. Below 0, the window is overspent. With fewer than 4 scored runs the window is shorter,
  so the first runs' budgets are small.

Everything is counted from the stored runs alone: no clock, no earlier build's output, and no roaster state from
after the run (the alias aside). The same raw gives the same rows, and building an older run again (`as_of_run`)
gives that run's numbers again, with the window ending at it; `tests/test_crawl_slo.py` checks both, and the
review's case of a store first crawled in a later run.

**Out of the gate.** The three models and their tests are tagged `report_only`: `coffeedata build` builds them in
the step after the gate, whose outcome never decides the build, and nothing in the gate reads them. Their data
tests are warnings for good. `mart_crawl_slo_profile` holds the run's metrics, which `quality.record_build` writes
to the builds table beside `mart_build_profile`'s; when the SLO's step fails, the build is recorded without them,
and prod-health shows them as "not measured". `tests/test_crawl_slo.py` breaks the measurement on purpose and
checks the gate still passes.

**The alert** (`config/prod_health.toml`, rule `crawl-slo`; fixed bounds, warnings only, no critical bound):

| Metric | Warns when | Meaning |
|---|---|---|
| `build:crawl_slo_met` | below 1 | the served release's run fetched 99% or less |
| `build:crawl_slo_burn_rate` | above 4 | that run alone spent about a window's budget or more |
| `build:crawl_slo_budget_remaining` | below 0 | the window is overspent, even if this run met the target |
| `build:crawl_slo_met:<platform>` | never by itself | `report only` in the issue's table: the platforms below 99%, so a miss points at its connector |

When it fires: read which platforms are marked `report only`, then the outcomes for the run, on a local copy of
the build's database (`select platform, outcome, count(*) from int_crawl_slo_roasters where counted and run_date
= '<run>' group by all`). A platform-wide failure is fetch-health's and the `fetch` rule's to act on; the SLO issue
stays open, updated each Tuesday, until a run is above 99% and the window is back within budget. Until the
served build's image has these models, the metrics show as "not measured" and nothing opens.

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
- **Claude's coverage** (`coverage:enrichment`, low side): judged like a fill rate, with `n` the listed coffees the
  rules abstained on (`rows:needs_llm`). The backlog only climbs, so the floor sits under the history's median and
  pages only on a real drop. Releases with no Claude row at all (from before the enrichment ran) are left out of its
  history, so their zeros don't sink the floor.
- **Abstain and changed shares** (high side): the median plus at least 0.02 for a warning and 0.05 for critical.
- **Volume ratios**: 1 ± at least 0.10 / 0.25. **Median shifts**: 0 ± at least 0.05 / 0.15.
- **Fixed, with no history**: a warn-only band test warns on any failure, and the classifier is judged on
  `min_verdict_share` 0.85. Release age warns above 7 days and is critical above 14. The crawl success SLO's
  bounds are fixed too ([above](#the-crawl-success-slo)); regenerating the file keeps them.

**Flagged history.** A history value outside its own warning band is not used to widen the band. It is listed as
`flagged` in the config, as a possible regression to look into. At revision 2026-11-12:
- `coverage:enrichment:square_online` and `:woocommerce` in run 2026-11-02 (0.014 each), below a band centred
  between it and run 2026-11-09. The backlog was earlier, not a regression;
- `shift:rows_ratio:listed:generic` was 1.17 between the 10-27 and 10-28 marts.

**The enrichment split (revision 2026-11-12).** Until then `abstain:enrichment` counted the coffees Claude had
filled in (`enrichment_method = 'llm'`). That measures the backlog, not the rules: in run 2026-11-09 WooCommerce
went from 0.013 to 0.196 because blocked stores reached the front of Claude's oldest-first queue, and nothing
regressed. It is now the rules' own abstain rate, from `dim_coffee.rules_complete` (`stg_products.needs_llm`, the
rules' `extract.needs_llm`). Releases built before that column are read from their values: Claude's rows (Claude is
only asked when the rules abstain) plus the rules' rows that fail the same test. Over the four releases in the
history it is 0.88–0.93 on every platform, flat. Claude's progress moved to `coverage:enrichment`, which went 0,
0, 0.046, 0.074 overall. The two zeros are left out of its history, so its floor is 0.045 overall (warning)
instead of 0.017. Judged with these bounds, the 11-09 release breaches neither.

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
  download that one ci run's report. importers-alerts' alert job reads only the artifact its own check job made.
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
  prod-health's `measure`, importers-alerts' `check`), and those jobs have no `issues: write`. importers-alerts'
  check only lists the importers dataset's file names (`triage importers-weeks`); it never fetches an importer and
  never dispatches `importers`.
- **Importers.** The issue, the log and the `importers-health` artifact carry importer keys, statuses, Mondays and
  run links, never a lot or a store's address.
- The Cloud Run jobs get no GitHub token, and no GCP resource was added. prod-health reads the lake read-only and
  never writes to it.
- To test without touching GitHub: `python -m coffeedata.triage evals <report.json> --workflow ci --dry-run`
  prints the issue it would open. `tests/test_triage.py` and `tests/test_prod_health.py` cover deduplication,
  updates, reopening and recovery against an in-memory GitHub, and the importers' check against a mock Hub.
