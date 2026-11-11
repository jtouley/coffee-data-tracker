"""Production-run health: per-rule proxy metrics of the served release, judged against versioned thresholds.

Production has no labels, so a rule's health is read from proxies (docs/TRIAGE.md):
- fill and abstain rates per rule and per platform, over the listed retail coffees of the served release's
  dim_coffee (is_listed, not wholesale): origin, process, roast level, bag grams, price_per_100g, the classifier's
  verdict coverage (classified_by = 'claude', once a release carries it), and the extraction rules' abstain rate
  (enrichment_method = 'llm': the rules left the coffee to Claude);
- distribution shift against the previous run's served release: listed volume ratio, the share of previous coffees
  still listed, the share of common coffees whose value changed, and the median shift of price_per_100g and grams;
- the build's own record (the lake's builds table): mart_build_profile's fetch shares and the dbt band tests that
  only warn (reused, not recomputed), and the served release's age.

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
# The extraction rules abstained and Claude filled the coffee in (high is bad).
ABSTAIN = {"enrichment": "coalesce(enrichment_method = 'llm', false)"}
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
# dbt tests that only warn, whose failures are worth a triage issue (status warn with failures > 0).
WARN_TESTS = (
    "build_metrics_in_band",
    "fill_and_freshness_in_band",
    "adapter_volume_in_band",
    "coverage_claims_match_build",
    "verdicts_cover_catalog",
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
    if "enrichment_method" in cols:
        exprs += [f'avg(({sql})::int) as "abstain:{r}"' for r, sql in ABSTAIN.items()]
    q = f"select platform, count(*) as n, {', '.join(exprs)} from {_read(dim_coffee)} where {_listed(cols)}"
    out: dict[str, float] = {}
    rows = con.execute(q + " group by grouping sets ((platform), ())").fetchall()
    names = [d[0] for d in con.description]
    for row in rows:
        rec = dict(zip(names, row, strict=True))
        platform = rec.pop("platform")
        suffix = "" if platform is None else f":{platform}"
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
        if value is not None and (metric in BUILD_SHARES or metric.startswith(BUILD_PREFIXES)):
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


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    cfg = tomllib.loads(path.read_text())
    if cfg.get("version") != 1:
        raise SystemExit(f"{path}: unsupported config version {cfg.get('version')!r}")
    return cfg


def judge(metrics: dict[str, float], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One result per configured metric: ok, warning or critical (or missing). Severity is the worst bound crossed."""
    results = []
    for m in cfg["metric"]:
        name, value = m["name"], metrics.get(m["name"])
        r = {"metric": name, "rule": m["rule"], "value": value, "baseline": m.get("baseline"), "status": "ok"}
        if value is None:
            r["status"] = "warning" if m.get("required") else "missing"
            r["why"] = "not measured in this release"
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
        results.append(r)
    return results


def alerts(
    measured: dict[str, Any], results: list[dict[str, Any]], run_url: str | None, sha: str | None
) -> list[dict]:
    """The triage alerts (coffeedata.triage's input), one per rule: firing with its breaches, or recovered."""
    by_rule: dict[str, list[dict]] = {}
    for r in results:
        if r["status"] != "missing":
            by_rule.setdefault(r["rule"], []).append(r)
    s = measured["served"]
    out = []
    for rule, rs in sorted(by_rule.items()):
        bad = [r for r in rs if r["status"] in ("warning", "critical")]
        out.append({
            "key": f"prod:{rule}",
            "kind": "prod",
            "title": f"prod health: {rule}",
            "firing": bool(bad),
            "severity": "critical" if any(r["status"] == "critical" for r in bad) else "warning",
            "summary": f"{len(bad)} metric(s) of `{rule}` breach their thresholds in release {s['build_id']}."
            if bad else "",
            "source": "config/prod_health.toml",
            "checks": [_check_line(r) for r in (bad or rs)],
            "context": {
                "served release": f"{s['build_id']} (run {s['run_date']})",
                "compared with": (s["previous"] or {}).get("build_id") or "no earlier run",
                "commit": sha,
                "run": run_url,
            },
        })  # fmt: skip
    return out


def _unit(metric: str) -> str:
    return "count" if metric.startswith(("rows:", "build:test:", "freshness:")) else "share"


def _scope(metric: str) -> str:
    """The platform a per-platform metric is about, else "all"."""
    parts = metric.split(":")
    if parts[0] in ("fill", "abstain", "rows") and len(parts) == 3:
        return parts[2]
    if metric.startswith(("shift:rows_ratio:listed:", "build:adapter_ok_share:")):
        return parts[-1]
    return "all"


def _check_line(r: dict) -> dict[str, Any]:
    return {
        "set": _scope(r["metric"]),
        "unit": _unit(r["metric"]),
        "name": r["metric"],
        "status": r["status"],
        "value": r["value"],
        "threshold": r.get("bound"),
        "op": r.get("op"),
        "baseline": r.get("baseline"),
        "why": r.get("why"),
    }


def summary(measured: dict[str, Any], results: list[dict[str, Any]]) -> str:
    """Every judged metric as a Markdown table, worst first: the run's page shows the week's health at a glance."""
    order = {"critical": 0, "warning": 1, "missing": 2, "ok": 3}
    s = measured["served"]
    lines = [
        f"### Production health: release {s['build_id']} (run {s['run_date']})",
        "",
        "| Status | Rule | Metric | Value | Bound | Baseline |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (order[r["status"]], r["rule"], r["metric"])):
        v = "–" if r["value"] is None else f"{r['value']:.4g}"
        bound = f"{r['op']} {r['bound']:.4g}" if r.get("bound") is not None else ""
        base = "" if r.get("baseline") is None else f"{r['baseline']:.4g}"
        lines.append(f"| {r['status']} | {r['rule']} | `{r['metric']}` | {v} | {bound} | {base} |")
    return "\n".join(lines) + "\n"


# --- baselines -------------------------------------------------------------------------------------------------

# How a bound is set from the history (docs/TRIAGE.md, "Thresholds"): the worst value seen, widened by the larger
# of k robust standard deviations (1.4826 x MAD) and a minimum margin. The margins follow the build's own bands
# (dbt_project.yml band_min_share_halfwidth 0.05) for a warning, three times that for critical.
MARGIN = {"warning": 0.05, "critical": 0.15}
K = {"warning": 3.0, "critical": 6.0}
VOLUME_MARGIN = {"warning": 0.10, "critical": 0.25}
CADENCE_DAYS = {"warning": 8.0, "critical": 15.0}
MIN_VERDICT_SHARE = 0.85  # dbt_project.yml min_verdict_share: verdicts_cover_catalog's gate


def _sigma(xs: list[float]) -> float:
    med = statistics.median(xs)
    return 1.4826 * statistics.median(abs(x - med) for x in xs)


def _rule_of(name: str) -> str:
    kind, _, rest = name.partition(":")
    head = rest.split(":")[0]
    if kind in ("fill", "abstain"):
        return head
    if kind == "rows" or name.startswith(("shift:rows_ratio", "shift:retained")):
        return "volume"
    if name.startswith(("shift:changed:", "shift:median:")):
        return rest.split(":")[1]
    if name.startswith("build:test:"):
        return "build-tests"
    if kind == "build":
        return "fetch" if name != "build:passed" else "build-tests"
    return kind


def bounds(name: str, history: list[float]) -> dict[str, Any]:
    """The warning and critical bounds of one metric from its history, and which side is bad."""
    lo, hi, sd = min(history), max(history), _sigma(history)
    b: dict[str, Any] = {}
    if name.startswith(("fill:", "shift:retained", "build:run", "build:adapter")):
        for sev in ("warning", "critical"):
            b[f"{sev}_below"] = round(max(0.0, lo - max(K[sev] * sd, MARGIN[sev])), 4)
    elif name.startswith(("abstain:", "shift:changed:")):
        for sev in ("warning", "critical"):
            b[f"{sev}_above"] = round(min(1.0, hi + max(K[sev] * sd, MARGIN[sev])), 4)
    elif name.startswith("shift:rows_ratio") or name.startswith("shift:median:"):
        for sev in ("warning", "critical"):
            m = max(K[sev] * sd, VOLUME_MARGIN[sev])
            # a ratio is centred on 1, a median shift on 0: the band always contains "no change"
            centre = 1.0 if name.startswith("shift:rows_ratio") else 0.0
            b[f"{sev}_below"] = round(min(lo, centre) - m, 4)
            b[f"{sev}_above"] = round(max(hi, centre) + m, 4)
    elif name.startswith("build:test:") or name == "build:passed":
        if name == "build:passed":
            b["warning_below"] = 1.0
        else:
            b["warning_above"] = round(hi, 4)
    return b


def baseline(
    releases: list[Path],
    builds: list[Path],
    served_builds: list[str],
    computed: str,
    labels: list[str] | None = None,
) -> tuple[dict[str, list[float]], str]:
    """Every metric's history over `releases` (oldest first; `labels` name them in the config) and consecutive
    pairs, and the config's TOML text."""
    hist: dict[str, list[float]] = {}
    for i, rel in enumerate(releases):
        ms = release_metrics(rel)
        if i:
            ms |= shift_metrics(rel, releases[i - 1])
        for k, v in ms.items():
            hist.setdefault(k, []).append(v)
    for b in served_builds:
        for k, v in build_metrics(builds, b).items():
            hist.setdefault(k, []).append(v)
    # Without history: a warn-only band test is judged on any failure, the classifier's coverage on the build's own
    # gate (dbt_project.yml min_verdict_share), and the release's age on the weekly cadence.
    fixed: dict[str, dict[str, Any]] = {f"build:test:{t}": {"warning_above": 0.0} for t in WARN_TESTS}
    fixed["freshness:served_age_days"] = {f"{k}_above": v for k, v in CADENCE_DAYS.items()}
    fixed["fill:classifier"] = {
        "warning_below": MIN_VERDICT_SHARE,
        "critical_below": MIN_VERDICT_SHARE - MARGIN["critical"],
    }
    lines = [
        "# Production-run health thresholds (coffeedata.prod_health; what each alert means: docs/TRIAGE.md).",
        "# Generated by `python -m coffeedata.prod_health baseline` from the release history in the lake, then",
        "# reviewed. Change a bound by regenerating from a longer history (and bump `revision`), never to silence an",
        "# alert. Bounds: the worst value seen, widened by the larger of k x 1.4826 x MAD and a minimum margin",
        f"# (share: warning {MARGIN['warning']}, critical {MARGIN['critical']}; volume and median ratios: "
        f"{VOLUME_MARGIN['warning']} / {VOLUME_MARGIN['critical']}).",
        "# Without history: a warn-only dbt band test warns on any failure, the classifier's coverage is judged",
        f"# on the build's own gate (min_verdict_share {MIN_VERDICT_SHARE}), the served release's age on the weekly",
        f"# cadence ({CADENCE_DAYS['warning']:g} / {CADENCE_DAYS['critical']:g} days).",
        "version = 1",
        f'revision = "{computed}"',
        f"releases = {json.dumps(labels or [str(r) for r in releases])}",
        f"builds = {json.dumps(served_builds)}",
        "",
    ]
    for name in sorted(set(hist) | set(fixed)):
        xs = hist.get(name, [])
        bnd = bounds(name, xs) if xs else fixed[name]
        if not bnd:
            continue
        lines += ["[[metric]]", f'name = "{name}"', f'rule = "{_rule_of(name)}"']
        if xs:
            lines += [
                f"baseline = {round(statistics.median(xs), 4)}",
                f"history = {json.dumps([round(x, 4) for x in xs])}",
            ]
        else:
            lines.append('basis = "no history yet: a fixed bound (see the comment above)"')
        if name.startswith("fill:") and name.count(":") == 1 and name != "fill:classifier":
            lines.append("required = true")
        lines += [f"{k} = {v}" for k, v in bnd.items()] + [""]
    return hist, "\n".join(lines)


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
        results = judge(measured["metrics"], load_config(a.config))
        for r in results:
            if r["status"] in ("warning", "critical"):
                print(
                    f"{r['status'].upper()} {r['rule']}: {r['metric']} = {r['value']} {r['op']} {r['bound']}"
                )
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
        _, text = baseline(a.releases, builds, a.served_build, today, a.label or None)
        a.out.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
