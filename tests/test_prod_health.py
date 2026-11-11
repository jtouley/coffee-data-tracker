"""The production health check (coffeedata.prod_health, docs/TRIAGE.md) over synthetic lakes: what it measures, how
the thresholds judge it, and the triage issues a breach opens and its recovery closes. Offline; no issue is created."""

import shutil
import tomllib
from datetime import date
from pathlib import Path

import duckdb
import pytest

from coffeedata import prod_health as ph
from coffeedata import triage

from .test_triage import FakeGitHub

COLUMNS = (
    "coffee_id varchar, platform varchar, is_listed boolean, is_wholesale boolean, origin_country varchar, "
    "process varchar, roast_level varchar, bag_sizes_g integer[], price_per_100g double, enrichment_method varchar, "
    "classified_by varchar"
)


def _coffee(i, platform="shopify", origin="Ethiopia", grams=(340,), price=6.0, listed=True, wholesale=False,
            method="rules", by="claude"):  # fmt: skip
    return (f"r:{i}", platform, listed, wholesale, origin, "washed", "light", list(grams) if grams else None,
            price, method, by)  # fmt: skip


def _write(path: Path, rows: list[tuple], columns: str = COLUMNS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"create table t ({columns})")
    if rows:
        con.executemany(f"insert into t values ({', '.join('?' * len(rows[0]))})", rows)
    con.execute(f"copy t to '{path}' (format parquet)")
    con.close()
    return path


def _lake(root: Path, previous: list[tuple], current: list[tuple], warn_failures: int = 0) -> Path:
    runs = [
        ("2026-11-02", "20261103T000000-aaaaaa", previous),
        ("2026-11-09", "20261110T000000-bbbbbb", current),
    ]
    for run_date, build, rows in runs:
        _write(ph.release_path(root, run_date, build), rows)
        _write(
            root / "builds" / f"run_date={run_date}" / f"build={build}.parquet",
            [
                (build, "build", "build", None, "pass", None, None),
                (build, "metric", "run_ok_share", None, None, None, 0.95),
                (build, "metric", "adapter_ok_share:shopify", None, None, None, 0.99),
                (build, "metric", "listed_coffees", None, None, None, 40000.0),
                (
                    build,
                    "node",
                    "test.coffeedata.fill_and_freshness_in_band",
                    "test",
                    "warn",
                    warn_failures,
                    None,
                ),
                (build, "node", "test.coffeedata.suspect_runs_need_review", "test", "warn", 6, None),
            ],
            "build_id varchar, kind varchar, unique_id varchar, resource_type varchar, status varchar, "
            "failures bigint, value double",
        )
        _write(
            root / "serving_log" / f"serve_id={build}.parquet",
            [(build, run_date, f"{run_date} 06:00:00+00")],
            "build_id varchar, run_date date, served_at timestamptz",
        )
    _write(
        root / "marts" / "release.parquet",
        [(runs[1][1], runs[1][0], "2026-11-10 06:00:00+00", "build for 2026-11-09")],
        "build_id varchar, run_date varchar, served_at timestamptz, reason varchar",
    )
    return root


def _healthy(n=100, offset=0):
    rows = [_coffee(i + offset) for i in range(n // 2)]
    rows += [
        _coffee(i + offset, platform="woocommerce", origin=None if i % 2 else "Kenya")
        for i in range(n // 2, n)
    ]
    rows += [_coffee(10_000 + i, wholesale=True, origin=None) for i in range(10)]  # not counted
    rows += [_coffee(20_000 + i, listed=False, origin=None) for i in range(10)]  # not counted
    return rows


def test_it_measures_fill_abstain_shift_and_the_build_record(tmp_path):
    prev = _healthy()
    cur = _healthy()
    cur[0] = _coffee(0, price=7.0)  # one changed price of 100 common coffees
    cur[1] = _coffee(1, method="llm")  # the rules abstained on one
    lake = _lake(tmp_path, prev, cur, warn_failures=3)
    m = ph.measure(lake, today=date(2026, 11, 10))
    assert m["served"]["previous"] == {"build_id": "20261103T000000-aaaaaa", "run_date": "2026-11-02"}
    x = m["metrics"]
    assert x["rows:listed"] == 100 and x["rows:listed:woocommerce"] == 50
    assert (
        x["fill:origin"] == 0.75 and x["fill:origin:shopify"] == 1.0 and x["fill:origin:woocommerce"] == 0.5
    )
    assert x["fill:grams"] == 1.0 and x["fill:price_per_100g"] == 1.0 and x["fill:classifier"] == 1.0
    assert x["abstain:enrichment"] == 0.01
    assert x["shift:rows_ratio:listed"] == 1.0 and x["shift:retained"] == 1.0
    assert x["shift:changed:price_per_100g"] == 0.01 and x["shift:changed:origin"] == 0.0
    assert x["shift:median:price_per_100g"] == 0.0
    assert x["build:run_ok_share"] == 0.95 and x["build:adapter_ok_share:shopify"] == 0.99
    assert "build:listed_coffees" not in x  # a volume metric the check measures itself
    assert (
        x["build:test:fill_and_freshness_in_band"] == 3.0 and "build:test:suspect_runs_need_review" not in x
    )
    assert x["build:passed"] == 1.0
    assert x["freshness:served_age_days"] == 1.0


def test_a_release_without_newer_columns_has_no_metric_for_them(tmp_path):
    old_cols = (
        "coffee_id varchar, platform varchar, is_listed boolean, origin_country varchar, process varchar"
    )
    p = _write(tmp_path / "old.parquet", [("r:1", "shopify", True, "Kenya", None)], old_cols)
    x = ph.release_metrics(p)
    assert x == {"rows:listed": 1.0, "rows:listed:shopify": 1.0, "fill:origin": 1.0, "fill:origin:shopify": 1.0,
                 "fill:process": 0.0, "fill:process:shopify": 0.0}  # fmt: skip


CONFIG = """
version = 1
[[metric]]
name = "fill:grams"
rule = "grams"
required = true
baseline = 0.95
warning_below = 0.9
critical_below = 0.8
[[metric]]
name = "shift:rows_ratio:listed"
rule = "volume"
baseline = 1.0
warning_below = 0.9
critical_below = 0.75
warning_above = 1.1
critical_above = 1.25
[[metric]]
name = "fill:classifier"
rule = "classifier"
warning_below = 0.85
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "cfg.toml"
    p.write_text(CONFIG)
    return ph.load_config(p)


@pytest.mark.parametrize(
    "metrics,expected",
    [
        ({"fill:grams": 0.95, "shift:rows_ratio:listed": 1.0}, {"grams": "ok", "volume": "ok"}),
        ({"fill:grams": 0.85, "shift:rows_ratio:listed": 1.2}, {"grams": "warning", "volume": "warning"}),
        ({"fill:grams": 0.5, "shift:rows_ratio:listed": 0.5}, {"grams": "critical", "volume": "critical"}),
        (
            {"shift:rows_ratio:listed": 1.3},
            {"grams": "warning", "volume": "critical"},
        ),  # required, not measured
    ],
)
def test_thresholds_have_a_severity_on_each_side(cfg, metrics, expected):
    got = {r["rule"]: r["status"] for r in ph.judge(metrics, cfg) if r["status"] != "missing"}
    assert got == expected


def test_an_unrequired_metric_not_measured_is_left_alone(cfg):
    r = next(r for r in ph.judge({}, cfg) if r["metric"] == "fill:classifier")
    assert r["status"] == "missing"
    rules = {
        a["key"]: a for a in ph.alerts({"served": _served()}, ph.judge({"fill:grams": 1.0}, cfg), None, None)
    }
    assert "prod:classifier" not in rules  # nothing measured: no alert, and no recovery either


def _served():
    return {
        "build_id": "b2",
        "run_date": "2026-11-09",
        "previous": {"build_id": "b1", "run_date": "2026-11-02"},
    }


def test_a_breach_opens_one_issue_per_rule_and_recovery_closes_it(cfg):
    gh = FakeGitHub()
    bad = ph.alerts(
        {"served": _served()}, ph.judge({"fill:grams": 0.7, "shift:rows_ratio:listed": 1.0}, cfg), "R1", "s1"
    )
    assert triage.sync(bad, gh, now="t") == [("opened", "prod:grams", 1)]
    issue = gh.store[0]
    assert issue["title"] == "[triage] prod health: grams"
    assert set(issue["labels"]) == {"triage", "prod-health", "severity:critical"}
    assert "| all | `fill:grams` | critical | 70.00% | below 80.00% | 95.00% |" in issue["body"]
    assert "served release: b2 (run 2026-11-09)" in issue["body"] and "- run: R1" in issue["body"]
    assert "When to roll back" in issue["body"]
    # next week, the same breach: updated in place, no new issue
    assert triage.sync(bad, gh, now="t") == [("updated", "prod:grams", 1)]
    good = ph.alerts(
        {"served": _served()}, ph.judge({"fill:grams": 0.96, "shift:rows_ratio:listed": 1.0}, cfg), "R2", "s2"
    )
    assert triage.sync(good, gh, now="t") == [("closed", "prod:grams", 1)]
    assert gh.store[0]["state"] == "closed" and len(gh.store) == 1


def test_a_check_that_could_not_measure_opens_its_own_issue():
    gh = FakeGitHub()
    assert triage.sync(triage.prod_pipeline_alert(False, "R", None), gh) == [
        ("opened", "pipeline:prod-health", 1)
    ]
    assert triage.sync(triage.prod_pipeline_alert(True, "R", None), gh) == [
        ("closed", "pipeline:prod-health", 1)
    ]


def test_fetch_only_downloads_the_served_and_previous_release(tmp_path):
    src = _lake(tmp_path / "remote", _healthy(), _healthy())
    calls = []

    def get(repo_id, repo_type, local_dir, allow_patterns):
        calls.append((repo_id, repo_type, allow_patterns))
        for pat in allow_patterns:
            for f in src.glob(pat.replace("/*", "/**/*") if pat.endswith("/*") else pat):
                if f.is_file():
                    dest = Path(local_dir) / f.relative_to(src)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(f, dest)

    s = ph.fetch("hf://datasets/someone/lake", tmp_path / "copy", get=get)
    assert s["build_id"] == "20261110T000000-bbbbbb"
    assert [c[:2] for c in calls] == [("someone/lake", "dataset")] * 2
    assert calls[1][2] == [
        "releases/run_date=2026-11-09/build=20261110T000000-bbbbbb/dim_coffee.parquet",
        "releases/run_date=2026-11-02/build=20261103T000000-aaaaaa/dim_coffee.parquet",
    ]
    assert ph.measure(tmp_path / "copy")["metrics"]["rows:listed"] == 100
    with pytest.raises(SystemExit):
        ph.fetch("s3://bucket/lake", tmp_path / "x", get=get)


def test_baselines_from_history_do_not_alert_on_that_history(tmp_path):
    weeks = [_healthy(100), _healthy(104), _healthy(98)]
    paths = [_write(tmp_path / f"w{i}.parquet", rows) for i, rows in enumerate(weeks)]
    hist, text = ph.baseline(paths, [], [], "2026-11-11", ["w0", "w1", "w2"])
    cfg = tomllib.loads(text)
    assert cfg["version"] == 1 and cfg["releases"] == ["w0", "w1", "w2"]
    for i, p in enumerate(paths):
        metrics = ph.release_metrics(p) | (ph.shift_metrics(p, paths[i - 1]) if i else {})
        breaches = [r for r in ph.judge(metrics, cfg) if r["status"] in ("warning", "critical")]
        assert breaches == [], breaches
    worse = _write(tmp_path / "bad.parquet", [_coffee(i, grams=None, price=None) for i in range(100)])
    breached = {
        r["metric"]: r["status"] for r in ph.judge(ph.release_metrics(worse), cfg) if r["status"] != "ok"
    }
    assert breached["fill:grams"] == "critical" and breached["fill:price_per_100g"] == "critical"


def test_the_checked_in_thresholds():
    """config/prod_health.toml: versioned, every metric has a rule and a bound, every overall fill rule is required,
    and no bound fires on the history it was computed from."""
    cfg = ph.load_config()
    assert cfg["version"] == 1 and cfg["revision"]
    names = [m["name"] for m in cfg["metric"]]
    assert len(names) == len(set(names))
    for m in cfg["metric"]:
        bounds = {k: v for k, v in m.items() if k.endswith(("_below", "_above"))}
        assert m["rule"] and bounds, m["name"]
        if m["name"].startswith("fill:") and m["name"].count(":") == 1 and m["name"] != "fill:classifier":
            assert m.get("required") is True, m["name"]
        for x in m.get("history", []):
            assert all(x >= v for k, v in bounds.items() if k.endswith("_below")), (m["name"], x)
            assert all(x <= v for k, v in bounds.items() if k.endswith("_above")), (m["name"], x)
        for side in ("below", "above"):
            w, c = m.get(f"warning_{side}"), m.get(f"critical_{side}")
            if w is not None and c is not None:
                assert (c <= w) if side == "below" else (c >= w), m["name"]
    for rule in (
        "origin",
        "process",
        "roast_level",
        "grams",
        "price_per_100g",
        "classifier",
        "enrichment",
        "volume",
    ):
        assert any(m["rule"] == rule for m in cfg["metric"]), rule
