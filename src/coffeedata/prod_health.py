"""Production-run health: per-rule proxy metrics of the served release, judged against versioned thresholds.

Production has no labels, so a rule's health is read from proxies (docs/TRIAGE.md):
- fill and abstain rates per rule and per platform, over the listed retail coffees of the served release's
  dim_coffee (is_listed, not wholesale): origin, process, roast level, bag grams, price_per_100g, the classifier's
  verdict coverage (classified_by = 'claude', once a release carries it), the extraction rules' abstain rate (the
  share the rules leave incompletely described, dim_coffee.rules_complete false, whether or not Claude has reached
  it) and Claude's coverage of those (the share with a Claude row: how far the weekly enrichment backlog has got);
- distribution shift against the previous run's served release: listed volume ratio, the share of previous coffees
  still listed, the share of common coffees whose value changed, and the median shift of price_per_100g and grams;
- the build's own record (the lake's builds table): mart_build_profile's fetch shares and the dbt band tests that
  only warn (reused, not recomputed), and the served release's age;
- the crawl success SLO (#96; dbt's mart_crawl_slo, read from the same record): whether the run fetched more than
  99% of the known roasters, how fast it spent its error budget and what is left of it. Warnings only.

    python -m coffeedata.prod_health fetch <lake-url> <dir>       # read-only download of what `measure` needs
    python -m coffeedata.prod_health measure <dir> <metrics.json> # the metrics of the served release
    python -m coffeedata.prod_health judge <metrics.json> [--config config/prod_health.toml] [--out alerts.json]
    python -m coffeedata.prod_health baseline <out.toml> <dim_coffee.parquet>... [--builds <dir>]

`judge` never fails on a breach: it writes the alerts for coffeedata.triage, which opens or updates the issues.
Nothing here writes to the lake or blocks the pipeline (ELT stays: no preconditions on loading).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
import tomllib
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("coffeedata.prod_health")

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "prod_health.toml"

# Rule -> the SQL that says a listed coffee has that rule's value. classified_by appears in releases built after
# 2026-11-10; a release without the column simply has no classifier metric.
FILLED = {
    "origin": "origin_country is not null",
    "process": "process is not null",
    "roast_level": "roast_level is not null",
    "grams": "len(coalesce(bag_sizes_g, []::integer[])) > 0",
    "price_per_100g": "price_per_100g is not null",
    "classifier": "coalesce(classified_by = 'claude', false)",
}
NEEDS = {
    "origin": "origin_country",
    "process": "process",
    "roast_level": "roast_level",
    "grams": "bag_sizes_g",
    "price_per_100g": "price_per_100g",
    "classifier": "classified_by",
}
# The extraction rules abstain: they leave the coffee incompletely described (extract.needs_llm), whether or not
# Claude has filled it in yet (high is bad). dim_coffee.rules_complete carries the rules' own answer. A release
# built before that column is read from what it has: Claude's rows (only coffees the rules left incomplete are sent),
# and the rules' rows whose own attributes are incomplete, by extract.needs_llm's test.
RULES_INCOMPLETE = (
    "not ((nullif(origin_country, '') is not null or coalesce(is_blend, false)) and nullif(process, '') is not null"
    " and len(coalesce(tasting_notes, [])) > 0)"
)


def _needs_llm(cols: set[str]) -> str | None:
    """The SQL that says the rules abstained on a listed coffee, or None if the release can't say."""
    if "rules_complete" in cols:
        return "not rules_complete"
    if {"enrichment_method", "origin_country", "is_blend", "process", "tasting_notes"} <= cols:
        return f"(coalesce(enrichment_method = 'llm', false) or {RULES_INCOMPLETE})"
    return None


# Rule -> the value compared between two releases, for the share of common coffees that changed.
# Rule -> (the column, the value compared, with {t} for the table alias).
COMPARED = {
    "origin": ("origin_country", "{t}.origin_country"),
    "process": ("process", "{t}.process"),
    "roast_level": ("roast_level", "{t}.roast_level"),
    "grams": ("bag_sizes_g", "{t}.bag_sizes_g"),
    "price_per_100g": ("price_per_100g", "round({t}.price_per_100g, 2)"),
}
# Rule -> (the column, the value whose median is compared).
MEDIANS = {
    "price_per_100g": ("price_per_100g", "price_per_100g"),
    "grams": ("bag_sizes_g", "list_min(bag_sizes_g)"),
}
# mart_build_profile metrics reused from the builds table (shares; low is bad).
BUILD_SHARES = ("run_ok_share", "run_catalog_ok_share")
BUILD_PREFIXES = ("adapter_ok_share:",)
# The crawl success SLO (#96), computed by dbt (mart_crawl_slo, recorded through mart_build_profile): judged on
# fixed bounds, warnings only. The target itself lives in dbt (var crawl_slo_target): crawl_slo_met is 1 when the
# run's share is above it. Per platform, crawl_slo_met:<platform> only reports (report_only): it names the
# platforms below the target in an alert the overall SLO opened, and never opens one itself.
SLO_METRICS = ("crawl_slo_met", "crawl_slo_burn_rate", "crawl_slo_budget_remaining")
SLO_PLATFORM_PREFIX = "crawl_slo_met:"
SLO_PLATFORMS = (
    "generic",
    "shopify",
    "square_online",
    "squarespace",
    "woocommerce",
)  # models.Roaster.adapter
# A run that burns error budget this many times faster than the target allows spends a whole window's budget by
# itself: dbt's crawl_slo_window_runs (tests/test_crawl_slo.py checks the two agree).
SLO_FAST_BURN = 4.0
SLO_REPORTED = "build:" + SLO_PLATFORM_PREFIX
SLO_BOUNDS: dict[str, dict[str, Any]] = {
    "build:crawl_slo_met": {"warning_below": 1.0},
    "build:crawl_slo_burn_rate": {"warning_above": SLO_FAST_BURN},
    "build:crawl_slo_budget_remaining": {"warning_below": 0.0},
}
# dbt tests that only warn, whose failures are worth a triage issue (status warn with failures > 0).
WARN_TESTS = (
    "build_metrics_in_band",
    "fill_and_freshness_in_band",
    "adapter_volume_in_band",
    "coverage_claims_match_build",
    "verdicts_cover_catalog",
    "origin_countries_are_mapped",  # a captured origin spelling the ISO seed doesn't have (published with no country)
)


def _connect():
    import duckdb

    return duckdb.connect()


def _read(path: Path) -> str:
    return f"read_parquet('{path}', hive_partitioning=false)"


def _columns(con, path: Path) -> set[str]:
    return {r[0] for r in con.execute(f"describe select * from {_read(path)}").fetchall()}


def _listed(cols: set[str]) -> str:
    """The listed retail coffees (as the site counts them); a release before is_wholesale has only retail."""
    return "is_listed and not coalesce(is_wholesale, false)" if "is_wholesale" in cols else "is_listed"


def release_metrics(dim_coffee: Path) -> dict[str, float]:
    """Volume, fill and abstain rates of one release's dim_coffee, overall and per platform."""
    con = _connect()
    cols = _columns(con, dim_coffee)
    # A column a release doesn't have yields no metric; for a required one (config) that is itself an alert.
    rules = {r: sql for r, sql in FILLED.items() if NEEDS[r] in cols}
    exprs = [f'avg(({sql})::int) as "fill:{r}"' for r, sql in rules.items()]
    if needs := _needs_llm(cols):
        # abstain: the rules' rate; coverage: the share of those Claude has filled in (low is bad: the backlog
        # lost ground); rows:needs_llm: its population, for the coverage bounds' standard error
        exprs += [
            f'avg(({needs})::int) as "abstain:enrichment"',
            f'count(*) filter (where {needs}) as "rows:needs_llm"',
            f"avg(coalesce(enrichment_method = 'llm', false)::int) filter (where {needs}) as \"coverage:enrichment\"",
        ]
    # grouping(platform) tells the catalog-wide row from a platform's; a listed coffee with no platform is its own
    # "unknown" row, never mistaken for the whole catalog
    q = (
        f"select grouping(platform) as overall, coalesce(platform, 'unknown') as platform, count(*) as n, "
        f"{', '.join(exprs)} from {_read(dim_coffee)} where {_listed(cols)}"
    )
    out: dict[str, float] = {}
    rows = con.execute(q + " group by grouping sets ((platform), ())").fetchall()
    names = [d[0] for d in con.description]
    for row in rows:
        rec = dict(zip(names, row, strict=True))
        platform, overall = rec.pop("platform"), rec.pop("overall")
        suffix = "" if overall else f":{platform}"
        out[f"rows:listed{suffix}"] = float(rec.pop("n"))
        for k, v in rec.items():
            if v is not None:
                out[f"{k}{suffix}"] = round(float(v), 6)
    con.close()
    return out


def shift_metrics(current: Path, previous: Path) -> dict[str, float]:
    """How the served release moved against the previous run's: volume, retention, changed values, medians."""
    con = _connect()
    cc, pc = _columns(con, current), _columns(con, previous)
    cur = f"(select * from {_read(current)} where {_listed(cc)})"
    prev = f"(select * from {_read(previous)} where {_listed(pc)})"
    out: dict[str, float] = {}
    (n_cur,) = con.execute(f"select count(*) from {cur}").fetchone()
    (n_prev,) = con.execute(f"select count(*) from {prev}").fetchone()
    if n_prev:
        out["shift:rows_ratio:listed"] = round(n_cur / n_prev, 6)
        (kept,) = con.execute(f"select count(*) from {prev} p semi join {cur} c using (coffee_id)").fetchone()
        out["shift:retained"] = round(kept / n_prev, 6)
    for platform, nc, np_ in con.execute(f"""
        select platform, count(*) filter (where side = 'c'), count(*) filter (where side = 'p')
        from (select platform, 'c' as side from {cur} union all select platform, 'p' from {prev})
        group by platform
    """).fetchall():
        if np_:
            out[f"shift:rows_ratio:listed:{platform}"] = round(nc / np_, 6)
    common = [r for r, (col, _) in COMPARED.items() if col in cc and col in pc]
    if common:
        exprs = ", ".join(
            f"avg(({COMPARED[r][1].format(t='c')} is distinct from {COMPARED[r][1].format(t='p')})::int)"
            for r in common
        )
        row = con.execute(f"select count(*), {exprs} from {cur} c join {prev} p using (coffee_id)").fetchone()
        if row and row[0]:
            for r, v in zip(common, row[1:], strict=True):
                out[f"shift:changed:{r}"] = round(float(v), 6)
    for r, (col, expr) in MEDIANS.items():
        if col in cc and col in pc:
            (mc,) = con.execute(f"select median({expr}) from {cur}").fetchone()
            (mp,) = con.execute(f"select median({expr}) from {prev}").fetchone()
            if mc is not None and mp:
                out[f"shift:median:{r}"] = round(float(mc) / float(mp) - 1, 6)
    con.close()
    return out


def build_metrics(builds: Iterable[Path], build_id: str) -> dict[str, float]:
    """The served build's own record: reused mart_build_profile shares, warn-only band tests that fired."""
    files = [str(p) for p in builds]
    if not files:
        return {}
    con = _connect()
    src = f"read_parquet({files!r}, hive_partitioning=false, union_by_name=true)"
    out: dict[str, float] = {}
    for metric, value in con.execute(
        f"select unique_id, value from {src} where kind = 'metric' and build_id = ?", [build_id]
    ).fetchall():
        if value is not None and (
            metric in BUILD_SHARES + SLO_METRICS or metric.startswith((*BUILD_PREFIXES, SLO_PLATFORM_PREFIX))
        ):
            out[f"build:{metric}"] = round(float(value), 6)
    for uid, failures in con.execute(
        f"select unique_id, failures from {src} where kind = 'node' and resource_type = 'test' and build_id = ?",
        [build_id],
    ).fetchall():
        name = uid.split(".")[-1]
        if name in WARN_TESTS:
            out[f"build:test:{name}"] = float(failures or 0)
    (status,) = con.execute(
        f"select coalesce(max(status), 'unknown') from {src} where kind = 'build' and build_id = ?",
        [build_id],
    ).fetchone()
    out["build:passed"] = 1.0 if status == "pass" else 0.0
    con.close()
    return out


# --- reading the lake (read-only) ----------------------------------------------------------------------------


def _hf_repo(lake_url: str) -> str:
    prefix = "hf://datasets/"
    if not lake_url.startswith(prefix):
        raise SystemExit(f"prod_health fetch reads an hf:// dataset lake, not {lake_url!r}")
    return lake_url.removeprefix(prefix).strip("/")


def served(lake: Path) -> dict[str, Any]:
    """The served release and the previous run's served release, from marts/release.parquet and serving_log."""
    con = _connect()
    row = con.execute(
        f"select build_id, cast(run_date as varchar), served_at from {_read(lake / 'marts' / 'release.parquet')}"
    ).fetchone()
    if row is None:
        raise SystemExit("no release is served")
    build_id, run_date, served_at = row
    prev = None
    log_glob = lake / "serving_log" / "*.parquet"
    if list((lake / "serving_log").glob("*.parquet")):
        prev_row = con.execute(
            f"""select build_id, cast(run_date as varchar) from read_parquet('{log_glob}', hive_partitioning=false,
                union_by_name=true) where build_id is not null and cast(run_date as date) < cast(? as date)
                order by served_at desc limit 1""",
            [run_date],
        ).fetchone()
        prev = {"build_id": prev_row[0], "run_date": prev_row[1]} if prev_row else None
    con.close()
    return {"build_id": build_id, "run_date": run_date, "served_at": str(served_at), "previous": prev}


def release_path(lake: Path, run_date: str, build_id: str) -> Path:
    return lake / "releases" / f"run_date={run_date}" / f"build={build_id}" / "dim_coffee.parquet"


def fetch(lake_url: str, into: Path, get=None) -> dict[str, Any]:
    """Download, read-only, the served pointer, the serving log, the builds table and the two releases' dim_coffee.
    Nothing is ever written to the lake."""
    if get is None:
        from huggingface_hub import snapshot_download as get
    repo = _hf_repo(lake_url)
    kw = {"repo_id": repo, "repo_type": "dataset", "local_dir": str(into)}
    get(allow_patterns=["marts/release.parquet", "serving_log/*", "builds/*"], **kw)
    s = served(into)
    wanted = [s] + ([s["previous"]] if s["previous"] else [])
    get(allow_patterns=[str(release_path(Path(), w["run_date"], w["build_id"])) for w in wanted], **kw)
    return s


def measure(lake: Path, today: date | None = None) -> dict[str, Any]:
    """Every metric of the served release, with what was measured."""
    s = served(lake)
    current = release_path(lake, s["run_date"], s["build_id"])
    metrics = release_metrics(current)
    if s["previous"]:
        previous = release_path(lake, s["previous"]["run_date"], s["previous"]["build_id"])
        if previous.exists():
            metrics |= shift_metrics(current, previous)
    metrics |= build_metrics(sorted((lake / "builds").glob("*/*.parquet")), s["build_id"])
    today = today or datetime.now(UTC).date()
    metrics["freshness:served_age_days"] = float((today - date.fromisoformat(s["run_date"])).days)
    return {"served": s, "metrics": metrics}


# --- thresholds ------------------------------------------------------------------------------------------------

EXPECTED = ROOT / "config" / "prod_health_expected.toml"
BREACH = ("warning", "critical")


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    cfg = tomllib.loads(path.read_text())
    if cfg.get("version") != 2:
        raise SystemExit(f"{path}: unsupported config version {cfg.get('version')!r}")
    return cfg


def load_expected(path: Path = EXPECTED) -> list[dict[str, Any]]:
    """The acknowledged shifts: a merged change that moves a metric on purpose (docs/TRIAGE.md)."""
    if not path.exists():
        return []
    doc = tomllib.loads(path.read_text())
    if doc.get("version") != 1:
        raise SystemExit(f"{path}: unsupported version {doc.get('version')!r}")
    for e in doc.get("expected", []):
        missing = {"metric", "change", "by", "from_run", "until_run", "pr", "why"} - set(e)
        if missing or e["change"] not in ("down", "up", "either"):
            raise SystemExit(
                f"{path}: entry {e.get('metric')!r} needs {sorted(missing) or 'change up/down/either'}"
            )
    return list(doc.get("expected", []))


def _expected(r: dict[str, Any], expected: list[dict[str, Any]], run_date: str | None) -> dict | None:
    """The acknowledged shift a breach matches: the metric (a glob), the release window, the direction, and a move
    from the baseline no bigger than announced. A bigger move, or one outside the window, still alerts."""
    from fnmatch import fnmatchcase

    for e in expected:
        if not fnmatchcase(r["metric"], e["metric"]):
            continue
        if run_date is None or not (str(e["from_run"]) <= run_date <= str(e["until_run"])):
            continue
        way = "down" if r["op"] == "<" else "up"
        if e["change"] not in (way, "either"):
            continue
        base = r.get("baseline")
        # a metric with a fixed bound has no baseline to measure the move from: never acknowledged, at any size
        if base is not None and abs(r["value"] - base) <= float(e["by"]):
            return e
    return None


def judge(
    metrics: dict[str, float],
    cfg: dict[str, Any],
    expected: list[dict[str, Any]] | None = None,
    run_date: str | None = None,
) -> list[dict[str, Any]]:
    """One result per configured metric: ok, warning or critical; expected (a breach an acknowledged change
    explains); or not_measured (a required one is a warning). Severity is the worst bound crossed. A `report_only`
    metric is judged the same way and marked: `alerts` never opens an issue for it alone."""
    results = []
    for m in cfg["metric"]:
        name, value = m["name"], metrics.get(m["name"])
        r: dict[str, Any] = {"metric": name, "rule": m["rule"], "value": value, "baseline": m.get("baseline")}
        r["status"] = "ok"
        if m.get("report_only"):
            r["report_only"] = True
        if value is None:
            r["status"] = "warning" if m.get("required") else "not_measured"
            r["why"] = (
                "required, but not measured in this release"
                if m.get("required")
                else "not in this build's record (a build image older than the test)"
                if name.startswith("build:test:")
                else "not measured"
            )
            results.append(r)
            continue
        for sev in ("critical", "warning"):
            lo, hi = m.get(f"{sev}_below"), m.get(f"{sev}_above")
            if lo is not None and value < lo:
                r |= {"status": sev, "bound": lo, "op": "<"}
                break
            if hi is not None and value > hi:
                r |= {"status": sev, "bound": hi, "op": ">"}
                break
        if r["status"] in BREACH and (e := _expected(r, expected or [], run_date)):
            r |= {"status": "expected", "pr": e["pr"], "why": e["why"], "was": r["status"]}
        results.append(r)
    return results


def alerts(
    measured: dict[str, Any], results: list[dict[str, Any]], run_url: str | None, sha: str | None
) -> list[dict]:
    """The triage alerts (coffeedata.triage's input), one per rule: firing with its breaches, or recovered with the
    checks that were measured. A metric not measured is left out, so it can never count as recovered. No measured
    value goes into an alert (the lake is private): the metric, its status and its bound. A report_only metric's
    breach is listed in the alert its rule's other metrics opened, and neither opens nor keeps one open itself."""
    by_rule: dict[str, list[dict]] = {}
    for r in results:
        if r["status"] != "not_measured":
            by_rule.setdefault(r["rule"], []).append(r)
    s = measured["served"]
    out = []
    for rule, rs in sorted(by_rule.items()):
        bad = [r for r in rs if r["status"] in BREACH and not r.get("report_only")]
        detail = [
            r for r in rs if r["status"] == "expected" or (r["status"] in BREACH and r.get("report_only"))
        ]
        shown = bad + detail if bad else rs
        out.append({
            "key": f"prod:{rule}",
            "kind": "prod",
            "title": f"prod health: {rule}",
            "firing": bool(bad),
            "severity": "critical" if any(r["status"] == "critical" for r in bad) else "warning",
            "summary": f"{len(bad)} metric(s) of `{rule}` breach their thresholds in release {s['build_id']}."
            if bad else "",
            "source": "config/prod_health.toml",
            "checks": [_check_line(r) for r in shown],
            "context": {
                "served release": f"{s['build_id']} (run {s['run_date']})",
                "compared with": (s["previous"] or {}).get("build_id") or "no earlier run",
                "commit": sha,
                "run": run_url,
            },
        })  # fmt: skip
    return out


def _scope(metric: str) -> str:
    """The platform a per-platform metric is about, else "all"."""
    parts = metric.split(":")
    if parts[0] in ("fill", "abstain", "coverage", "rows") and len(parts) == 3:
        return parts[2]
    if metric.startswith(
        ("shift:rows_ratio:listed:", "build:adapter_ok_share:", "build:" + SLO_PLATFORM_PREFIX)
    ):
        return parts[-1]
    return "all"


def _check_line(r: dict) -> dict[str, Any]:
    status = r["status"] if r["status"] != "warning" or "bound" in r else "not measured (required)"
    if r.get("report_only") and r["status"] in BREACH:
        status = "report only"  # one of triage.OK: past its bound, shown, and not a failing check
    return {
        "set": _scope(r["metric"]),
        "name": r["metric"],
        "status": status,
        "threshold": r.get("bound"),
        "op": r.get("op"),
        "unit": "count"
        if r["metric"].startswith(("rows:", "build:test:", "freshness:", "build:crawl_slo_burn_rate"))
        else "share",
        "pr": r.get("pr"),
    }


def summary(measured: dict[str, Any], results: list[dict[str, Any]]) -> str:
    """Every judged metric as a Markdown table, worst first, with its status and bound but not its value (the run
    page is public; the values stay in the job: docs/TRIAGE.md)."""
    order = {"critical": 0, "warning": 1, "expected": 2, "not_measured": 3, "ok": 4}
    s = measured["served"]
    lines = [
        f"### Production health: release {s['build_id']} (run {s['run_date']})",
        "",
        "| Status | Rule | Metric | Bound crossed | Note |",
        "|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (order[r["status"]], r["rule"], r["metric"])):
        bound = f"{r['op']} {r['bound']:.4g}" if r.get("bound") is not None else ""
        note = f"#{r['pr']}: {r['why']}" if r.get("pr") else r.get("why", "")
        if r.get("report_only"):
            note = "report only: never opens an issue by itself"
        lines.append(f"| {r['status']} | {r['rule']} | `{r['metric']}` | {bound} | {note} |")
    return "\n".join(lines) + "\n"


# --- baselines -------------------------------------------------------------------------------------------------

# How a bound is set from the history (docs/TRIAGE.md, "Thresholds"). The centre is the history's median, never its
# worst value, so a regression already in the history doesn't widen the band; values outside the warning band are
# listed as `flagged` instead. The margin is the largest of:
#   - a relative drop from the median (REL), so a 3-point drop on a 96% fill rate warns;
#   - K_SE binomial standard errors at the population size n, so a small platform isn't paged on noise;
#   - K_MAD robust standard deviations of the history (1.4826 x MAD), for metrics that move week to week.
# A low-fill platform keeps an absolute floor: a drop of a quarter always warns, and of more than 45% (so a
# halving) is critical.
# Claude's coverage is judged like a fill rate (low side; n is the coffees the rules abstained on). Shares where only
# a rise is bad (the rules' abstain rate, changed values) use an absolute margin above the median instead; ratios
# centre on 1, median shifts on 0.
REL = {"warning": 0.03, "critical": 0.10}
K_SE = {"warning": 4.0, "critical": 8.0}
K_MAD = {"warning": 3.0, "critical": 6.0}
CAP = {"warning": 0.25, "critical": 0.45}  # the most a margin can be, relative to the median
LOW_MARGIN = {"warning": 0.02, "critical": 0.05}  # abstain and changed shares
RATIO_MARGIN = {"warning": 0.10, "critical": 0.25}  # volume ratios
MEDIAN_MARGIN = {"warning": 0.05, "critical": 0.15}  # median shifts
CADENCE_DAYS = {"warning": 7.0, "critical": 14.0}  # served release older than a week: a missed weekly run
MIN_VERDICT_SHARE = 0.85  # dbt_project.yml min_verdict_share: verdicts_cover_catalog's gate


def _sigma(xs: list[float]) -> float:
    med = statistics.median(xs)
    return 1.4826 * statistics.median(abs(x - med) for x in xs)


def _rule_of(name: str) -> str:
    kind, _, rest = name.partition(":")
    head = rest.split(":")[0]
    if kind in ("fill", "abstain", "coverage"):
        return head
    if kind == "rows" or name.startswith(("shift:rows_ratio", "shift:retained")):
        return "volume"
    if name.startswith(("shift:changed:", "shift:median:")):
        return rest.split(":")[1]
    if name.startswith("build:test:") or name == "build:passed":
        return "build-tests"
    if name.startswith("build:crawl_slo_"):
        return "crawl-slo"
    if kind == "build":
        return "fetch"
    return kind


def bounds(name: str, history: list[float], n: float | None = None) -> dict[str, Any]:
    """The warning and critical bounds of one metric from its history (and population size n), and the bad side."""
    m, sd = statistics.median(history), _sigma(history)
    se = math.sqrt(m * (1 - m) / n) if n and 0 < m < 1 else 0.0
    b: dict[str, Any] = {}
    if name.startswith(("fill:", "coverage:", "shift:retained", "build:run", "build:adapter")):
        for sev in ("warning", "critical"):
            margin = min(max(REL[sev] * m, K_SE[sev] * se, K_MAD[sev] * sd), CAP[sev] * m)
            b[f"{sev}_below"] = round(max(0.0, m - margin), 4)
        b["critical_below"] = min(b["critical_below"], b["warning_below"])
    elif name.startswith(("abstain:", "shift:changed:")):
        for sev in ("warning", "critical"):
            b[f"{sev}_above"] = round(min(1.0, m + max(LOW_MARGIN[sev], K_SE[sev] * se, K_MAD[sev] * sd)), 4)
    elif name.startswith(("shift:rows_ratio", "shift:median:")):
        centre = 1.0 if name.startswith("shift:rows_ratio") else 0.0
        floor = RATIO_MARGIN if centre else MEDIAN_MARGIN
        for sev in ("warning", "critical"):
            margin = max(floor[sev], K_MAD[sev] * sd)
            b[f"{sev}_below"] = round(centre - margin, 4)
            b[f"{sev}_above"] = round(centre + margin, 4)
    elif name == "build:passed":
        b["warning_below"] = 1.0
    elif name.startswith("build:test:"):
        b["warning_above"] = 0.0
    return b


def _outside(x: float, b: dict[str, Any]) -> bool:
    lo, hi = b.get("warning_below"), b.get("warning_above")
    return (lo is not None and x < lo) or (hi is not None and x > hi)


def _n_for(name: str, hist: dict[str, list[float]]) -> float | None:
    """The population a share is over: the listed coffees of its platform (or all), or for coverage the listed
    coffees the rules abstained on, as a median over the history."""
    if not name.startswith(("fill:", "abstain:", "coverage:", "shift:changed:", "shift:retained")):
        return None
    platform = _scope(name)
    base = "rows:needs_llm" if name.startswith("coverage:") else "rows:listed"
    counts = hist.get(base if platform == "all" else f"{base}:{platform}")
    return statistics.median(counts) if counts else None


def baseline(
    releases: list[Path],
    builds: list[Path],
    served_builds: list[str],
    computed: str,
    labels: list[str] | None = None,
) -> tuple[dict[str, list[float]], str, list[str]]:
    """Every metric's history over `releases` (oldest first; `labels` name them) and consecutive pairs, the config's
    TOML text, and the history values that fall outside their own warning band (possible regressions to look at,
    not baked into the bounds)."""
    labels = labels or [str(r) for r in releases]
    hist: dict[str, list[float]] = {}
    where: dict[str, list[str]] = {}
    for i, rel in enumerate(releases):
        ms = release_metrics(rel)
        if ms.get("coverage:enrichment") == 0:
            # no Claude row in the whole catalog: a release from before the enrichment ran says nothing about the
            # backlog, and its zeros would sink the coverage floor, so it is left out of every coverage history
            ms = {k: v for k, v in ms.items() if not k.startswith("coverage:")}
        if i:
            ms |= {k: v for k, v in shift_metrics(rel, releases[i - 1]).items()}
        for k, v in ms.items():
            hist.setdefault(k, []).append(v)
            where.setdefault(k, []).append(
                labels[i] if not k.startswith("shift:") else f"{labels[i - 1]} -> {labels[i]}"
            )
    for b in served_builds:
        for k, v in build_metrics(builds, b).items():
            hist.setdefault(k, []).append(v)
            where.setdefault(k, []).append(f"build {b}")
    # Without history: a warn-only band test warns on any failure, the classifier's coverage is judged on the
    # build's own gate, and the release's age on the weekly cadence.
    fixed: dict[str, dict[str, Any]] = {f"build:test:{t}": {"warning_above": 0.0} for t in WARN_TESTS}
    fixed["freshness:served_age_days"] = {f"{k}_above": v for k, v in CADENCE_DAYS.items()}
    fixed["fill:classifier"] = {
        "warning_below": MIN_VERDICT_SHARE,
        "critical_below": MIN_VERDICT_SHARE - 0.15,
    }
    # The crawl success SLO (#96): fixed bounds, whatever the history says. dbt decides `met` against its target.
    fixed |= SLO_BOUNDS
    for platform in sorted(
        {*SLO_PLATFORMS, *(n.rpartition(":")[2] for n in hist if n.startswith(SLO_REPORTED))}
    ):
        fixed[SLO_REPORTED + platform] = {"report_only": True, "warning_below": 1.0}
    lines = [
        "# Production-run health thresholds (coffeedata.prod_health; what each alert means: docs/TRIAGE.md).",
        "# Generated by `python -m coffeedata.prod_health baseline` from the release history in the lake, then",
        "# reviewed. Change a bound by regenerating from a longer history (and bump `revision`), never to silence an",
        "# alert; an intended shift goes in config/prod_health_expected.toml.",
        "# Bounds centre on the history's median. Margin: the largest of a relative drop "
        f"({REL['warning']:g} / {REL['critical']:g}),",
        f"# {K_SE['warning']:g} / {K_SE['critical']:g} binomial standard errors at the platform's n, and "
        f"{K_MAD['warning']:g} / {K_MAD['critical']:g} x 1.4826 x MAD, capped at",
        f"# {CAP['warning']:g} / {CAP['critical']:g} of the median (fill and coverage shares, low side). Abstain and "
        f"changed shares: median + {LOW_MARGIN['warning']:g} / {LOW_MARGIN['critical']:g} at least;",
        f"# volume ratios: 1 +/- {RATIO_MARGIN['warning']:g} / {RATIO_MARGIN['critical']:g} at least; median shifts: "
        f"0 +/- {MEDIAN_MARGIN['warning']:g} / {MEDIAN_MARGIN['critical']:g} at least.",
        "# Claude's coverage leaves out releases with no Claude row at all (from before the enrichment ran).",
        "# Without history: a warn-only dbt band test warns on any failure, the classifier's coverage is judged",
        f"# on the build's own gate (min_verdict_share {MIN_VERDICT_SHARE}), the served release's age on the weekly",
        f"# cadence (above {CADENCE_DAYS['warning']:g} days: a missed week; above {CADENCE_DAYS['critical']:g}: two).",
        "# `flagged`: history values outside their own warning band, possible regressions to look at.",
        "# The crawl success SLO (rule crawl-slo, #96; docs/TRIAGE.md): fixed bounds, warnings only. met is 1 when",
        "# the run fetched more than dbt's crawl_slo_target (0.99) of the known roasters; burn_rate above "
        f"{SLO_FAST_BURN:g}: one run",
        "# spent a whole window's error budget; budget_remaining below 0: the window is overspent. `report_only`:",
        "# listed in an alert another metric of its rule opened, never opening one itself (the per-platform met).",
        "version = 2",
        f'revision = "{computed}"',
        f"releases = {json.dumps(labels)}",
        f"builds = {json.dumps(served_builds)}",
        "",
    ]
    flagged_all: list[str] = []
    for name in sorted(set(hist) | set(fixed)):
        xs = hist.get(name, [])
        n = _n_for(name, hist)
        bnd = bounds(name, xs, n) if xs and name not in fixed else fixed.get(name, {})
        if not bnd:
            continue
        lines += ["[[metric]]", f'name = "{name}"', f'rule = "{_rule_of(name)}"']
        if xs and name not in fixed:
            flagged = [f"{w} ({x:.4g})" for x, w in zip(xs, where[name], strict=True) if _outside(x, bnd)]
            flagged_all += [f"{name}: {f}" for f in flagged]
            lines += [
                f"baseline = {round(statistics.median(xs), 4)}",
                f"history = {json.dumps([round(x, 4) for x in xs])}",
            ]
            if n:
                lines.append(f"n = {round(n)}")
            if flagged:
                lines.append(f"flagged = {json.dumps(flagged)}")
        else:
            lines.append('basis = "no history yet: a fixed bound (see the comment above)"')
        if name.startswith("fill:") and name.count(":") == 1 and name != "fill:classifier":
            lines.append("required = true")
        lines += [f"{k} = {json.dumps(v)}" for k, v in bnd.items()] + [""]
    return hist, "\n".join(lines), flagged_all


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m coffeedata.prod_health", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("lake_url")
    f.add_argument("dir", type=Path)
    m = sub.add_parser("measure")
    m.add_argument("dir", type=Path)
    m.add_argument("out", type=Path)
    j = sub.add_parser("judge")
    j.add_argument("metrics", type=Path)
    j.add_argument("--config", type=Path, default=CONFIG)
    j.add_argument("--expected", type=Path, default=EXPECTED)
    j.add_argument("--out", type=Path)
    j.add_argument(
        "--summary", type=Path, help="append a Markdown table of every result (GITHUB_STEP_SUMMARY)"
    )
    b = sub.add_parser("baseline")
    b.add_argument("out", type=Path)
    b.add_argument(
        "releases", type=Path, nargs="+", help="dim_coffee.parquet of each served run, oldest first"
    )
    b.add_argument("--builds", type=Path, help="the lake's builds folder")
    b.add_argument("--served-build", action="append", default=[], help="a served build id (repeat)")
    b.add_argument("--label", action="append", default=[], help="each release's name in the config (repeat)")
    a = p.parse_args(argv)
    if a.cmd == "fetch":
        s = fetch(a.lake_url, a.dir)
        log.info("served %s (run %s); previous %s", s["build_id"], s["run_date"], s["previous"])
    elif a.cmd == "measure":
        a.out.write_text(json.dumps(measure(a.dir), indent=1, sort_keys=True) + "\n")
    elif a.cmd == "judge":
        import os

        from . import lineage

        measured = json.loads(a.metrics.read_text())
        expected = load_expected(a.expected)
        results = judge(measured["metrics"], load_config(a.config), expected, measured["served"]["run_date"])
        for r in results:  # names and bounds only: the log is public
            if r["status"] in BREACH:
                print(
                    f"{r['status'].upper()} {r['rule']}: {r['metric']} {r.get('op', '')} {r.get('bound', r.get('why'))}"
                )
            elif r["status"] == "expected":
                print(f"EXPECTED (#{r['pr']}) {r['rule']}: {r['metric']} {r['op']} {r['bound']}")
        firing = alerts(measured, results, lineage.ci_run()[1], os.environ.get("GITHUB_SHA"))
        if a.out:
            a.out.write_text(json.dumps(firing, indent=1) + "\n")
        if a.summary:
            with a.summary.open("a") as f:
                f.write(summary(measured, results))
        n = sum(x["firing"] for x in firing)
        print(f"{n} of {len(firing)} rules breach a threshold")
    else:
        builds = sorted(a.builds.glob("*/*.parquet")) if a.builds else []
        today = datetime.now(UTC).date().isoformat()
        _, text, flagged = baseline(a.releases, builds, a.served_build, today, a.label or None)
        a.out.write_text(text)
        for line in flagged:
            print(f"flagged {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
