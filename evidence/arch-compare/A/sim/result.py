"""Assemble A/result.json from the simulated weeks (/tmp/A_all.json from analyze.py) and the real crawl summary."""
import json, os, sys
A = "/home/user/coffee-data-tracker/build/arch-compare/A"
weeks = json.load(open(sys.argv[1]))
real = json.load(open(f"{A}/real/metrics.json")) if os.path.exists(f"{A}/real/metrics.json") else None
res = {
 "design": "A: one weekly omnibus crawl (Mon) + resume-only catch-up of the same workflow (Tue); per-store Shopify "
           "backoff + deferred rounds (#62), 170-min fetch deadline, open_run resume, refused labelling.",
 "simulation": {
   "how": "build/arch-compare/A/sim: mockweek.py (copy of crawl-plan mockserver + SCENARIO faults), weekjob.py runs the "
          "worktree's ingest.open_run/select_states/run(deadline)/retry-failed pass, runweek.py drives the week, "
          "analyze.py computes metrics. 2,545 profiles, clock x10, 32 workers. Probe, enrich, dbt build not simulated; "
          "gates approximated in SQL (run_is_complete, run_ok_rate 0.9, adapter_ok_rate 0.5).",
   "placements": {"calendar": "faults on the SCENARIO days: day-3 Woo outage and day-5 kill find no A job running",
                  "drill": "same faults moved onto A's Monday job: Woo 503 for its first 2 h, SIGKILL at 50%; Tuesday resumes"},
   "caveats": ["4 cores shared with other agents' sims: scaled wall times are upper-ish estimates",
               "job wall = ingest only (main + deferred rounds + retry-failed pass); discovery/probe/enrich/dbt add time",
               "S2 bucket counts every Shopify request (429s too) over a trailing 60 s"],
   "weeks": weeks},
 "real_crawl": real,
 "code": {"src_lines_changed": "+318 / -27 (ingest.py, ratelimit.py, cli.py, http.py); .github weekly.yml +8 / -2",
          "tests": "+444 lines (test_ratelimit, test_resume, test_lineage); full suite 656 passed, 3 skipped",
          "dbt_changes": 0, "app_changes": 0, "contracts_changes": 0,
          "worktree": "build/arch-compare/A/wt, branch arch-a (local, not pushed)"},
 "extensibility_bigcartel": {
   "files": ["src/coffeedata/adapters/bigcartel.py (new, ~60-100 lines)", "adapters/__init__.py ADAPTERS (+2)",
             "models.py Platform literal + Roaster.adapter mapping (+2)", "discover.py signature + probe endpoint (+2-3)",
             "ratelimit.py BIGCARTEL_RPS/CONCURRENCY (+2)", "ingest.run: second PlatformLimiter + limiters dict (+4)"],
   "config_touch_points": 2, "dbt": "none needed (adapter comes from fetch rows; run_completeness's fallback mapping "
   "would label never-fetched bigcartel roasters 'generic' until first fetch)",
   "note": "the new limiter shares the one job's 32 workers and 170-min budget with every other connector"},
 "operational_surface": {"scheduled_entries": 2, "jobs": 1,
   "entries": ["weekly Mon 09:17 UTC (full run)", "catch-up Tue 09:17 UTC (weekly --resume-only, no-op if no open run)"],
   "owner_watches": ["weekly job exit status (non-zero = stopped at deadline or gate failed)",
                     "Tuesday catch-up outcome (if it also stops, the week is unserved)",
                     "refused count and Shopify ok-share in the run summary", "freshness: no passing run in 8 days"]},
}
json.dump(res, open(f"{A}/result.json", "w"), indent=1, default=str)
print("ok")
