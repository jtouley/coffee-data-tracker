"""The production health check (coffeedata.prod_health, docs/TRIAGE.md) over synthetic lakes: what it measures, how
the thresholds judge it, and the triage issues a breach opens and its recovery closes. Offline; no issue is created."""

import json
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
    "classified_by varchar, rules_complete boolean"
)


def _coffee(i, platform="shopify", origin="Ethiopia", grams=(340,), price=6.0, listed=True, wholesale=False,
            method="rules", by="claude", complete=None):  # fmt: skip
    complete = (
        method == "rules" if complete is None else complete
    )  # Claude is only asked when the rules abstain
    return (f"r:{i}", platform, listed, wholesale, origin, "washed", "light", list(grams) if grams else None,
            price, method, by, complete)  # fmt: skip


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
    assert x["abstain:enrichment"] == 0.01 and x["rows:needs_llm"] == 1 and x["coverage:enrichment"] == 1.0
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
version = 2
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
    got = {r["rule"]: r["status"] for r in ph.judge(metrics, cfg) if r["status"] != "not_measured"}
    assert got == expected


def test_an_unrequired_metric_not_measured_is_left_alone(cfg):
    r = next(r for r in ph.judge({}, cfg) if r["metric"] == "fill:classifier")
    assert r["status"] == "not_measured"
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
    # the lake is private: the issue names the check and its bound, never the measured value or the baseline
    assert "| all | `fill:grams` | critical | below 80.00% |" in issue["body"]
    assert "70" not in issue["body"] and "95.00%" not in issue["body"]
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
    assert "prod-health" in gh.store[0]["labels"] and "eval-regression" not in gh.store[0]["labels"]
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
    hist, text, flagged = ph.baseline(paths, [], [], "2026-11-11", ["w0", "w1", "w2"])
    cfg = tomllib.loads(text)
    assert cfg["version"] == 2 and cfg["releases"] == ["w0", "w1", "w2"] and flagged == []
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
    bounds centre on the median, and a history value outside its own warning band is listed as flagged (a possible
    regression to look at), never silently widening the band."""
    cfg = ph.load_config()
    assert cfg["version"] == 2 and cfg["revision"]
    names = [m["name"] for m in cfg["metric"]]
    assert len(names) == len(set(names))
    for m in cfg["metric"]:
        bounds = {k: v for k, v in m.items() if k.endswith(("_below", "_above"))}
        assert m["rule"] and bounds, m["name"]
        if m["name"].startswith("fill:") and m["name"].count(":") == 1 and m["name"] != "fill:classifier":
            assert m.get("required") is True, m["name"]
        outside = [x for x in m.get("history", []) if ph._outside(x, bounds)]
        assert len(outside) == len(m.get("flagged", [])), m["name"]
        for side in ("below", "above"):
            w, c = m.get(f"warning_{side}"), m.get(f"critical_{side}")
            if w is not None and c is not None:
                assert (c <= w) if side == "below" else (c >= w), m["name"]
    for rule in ("origin", "process", "roast_level", "grams", "price_per_100g", "classifier", "enrichment", "volume",
                 "fetch", "build-tests", "freshness"):  # fmt: skip
        assert any(m["rule"] == rule for m in cfg["metric"]), rule
    for t in ph.WARN_TESTS:  # every warn-only band test is judged, whether or not a build has recorded it yet
        assert f"build:test:{t}" in names


def _base(cfg, name):
    return next(m for m in cfg["metric"] if m["name"] == name)["baseline"]


@pytest.mark.parametrize(
    "metric,drop,status",
    [
        ("fill:grams:shopify", 0.03, "warning"),  # a 3-point drop on a ~96% fill rate
        ("fill:grams:shopify", 0.04, "warning"),
        ("fill:grams:shopify", 0.066, "warning"),  # #136's size
        ("fill:grams:shopify", 0.15, "critical"),
        ("fill:origin", 0.06, "warning"),
        ("fill:grams:square_online", 0.5, "critical"),  # a halving on a low-fill platform
        ("fill:price_per_100g:square_online", 0.5, "critical"),
    ],
)
def test_the_checked_in_bounds_catch_moderate_drops(metric, drop, status):
    """The reviewer's simulation, on the checked-in bounds: `drop` is absolute (points) for a high-fill metric and
    relative (a halving) for the low-fill platform."""
    cfg = ph.load_config()
    base = _base(cfg, metric)
    value = base * (1 - drop) if base < 0.5 else base - drop
    r = next(r for r in ph.judge({metric: value}, cfg) if r["metric"] == metric)
    assert r["status"] == status, (metric, base, value, r)


def test_noise_at_the_baseline_does_not_page():
    cfg = ph.load_config()
    for m in cfg["metric"]:
        if "baseline" in m and m["name"].startswith("fill:"):
            r = next(r for r in ph.judge({m["name"]: m["baseline"] - 0.005}, cfg) if r["metric"] == m["name"])
            assert r["status"] == "ok", m["name"]


def test_freshness_alerts_after_one_missed_week():
    cfg = ph.load_config()
    status = {d: next(r for r in ph.judge({"freshness:served_age_days": d}, cfg) if r["rule"] == "freshness")["status"]
              for d in (1.0, 7.0, 8.0, 15.0)}  # fmt: skip
    assert status == {1.0: "ok", 7.0: "ok", 8.0: "warning", 15.0: "critical"}


def _post_136(cfg):
    """The first release after #136, as its PR measured it: Shopify grams -6.6 points, 22% of bag sizes and 15% of
    prices per 100 g changed."""
    return {
        "fill:grams:shopify": _base(cfg, "fill:grams:shopify") - 0.066,
        "fill:price_per_100g:shopify": _base(cfg, "fill:price_per_100g:shopify") - 0.05,
        "fill:grams": _base(cfg, "fill:grams") + 0.01,
        "fill:price_per_100g": _base(cfg, "fill:price_per_100g") + 0.018,
        "shift:changed:grams": 0.22,
        "shift:changed:price_per_100g": 0.15,
    }


def test_an_acknowledged_change_is_reported_not_paged():
    cfg, expected = ph.load_config(), ph.load_expected()
    results = ph.judge(_post_136(cfg), cfg, expected, "2026-11-16")
    judged = {r["metric"]: r for r in results if r["metric"] in _post_136(cfg) and r["status"] != "ok"}
    assert len(judged) == 4
    assert {r["status"] for r in judged.values()} == {"expected"}
    assert {r["pr"] for r in judged.values()} == {136}
    out = ph.alerts({"served": _served()}, results, None, None)
    assert not any(a["firing"] for a in out if a["key"] in ("prod:grams", "prod:price_per_100g"))
    assert "expected" in ph.summary({"served": _served()}, results) and "#136" in ph.summary(
        {"served": _served()}, results
    )
    # outside the window (a week later for the one-off shifts), or a bigger move than announced, still alerts
    later = {r["metric"]: r["status"] for r in ph.judge(_post_136(cfg), cfg, expected, "2026-11-23")}
    assert later["shift:changed:grams"] == "critical" and later["fill:grams:shopify"] == "expected"
    worse = _post_136(cfg) | {"fill:grams:shopify": _base(cfg, "fill:grams:shopify") - 0.2}
    assert (
        next(r for r in ph.judge(worse, cfg, expected, "2026-11-16") if r["metric"] == "fill:grams:shopify")[
            "status"
        ]
        == "critical"
    )


def test_a_fixed_bound_is_never_acknowledged(cfg):
    """A metric with no baseline (freshness, a band test, the classifier) can't be measured against `by`: an entry
    for it never matches, whatever its size."""
    entry = {"metric": "fill:classifier", "change": "down", "by": 1.0, "from_run": "2026-01-01",
             "until_run": "2026-12-31", "pr": 1, "why": "test"}  # fmt: skip
    r = next(
        r
        for r in ph.judge({"fill:classifier": 0.1}, cfg, [entry], "2026-11-16")
        if r["metric"] == "fill:classifier"
    )
    assert r["status"] == "warning"


def test_a_listed_coffee_with_no_platform_keeps_the_catalog_wide_numbers(tmp_path):
    rows = [_coffee(i) for i in range(9)] + [_coffee(99, platform=None, origin=None)]
    x = ph.release_metrics(_write(tmp_path / "d.parquet", rows))
    assert x["rows:listed"] == 10 and x["fill:origin"] == 0.9
    assert x["rows:listed:unknown"] == 1 and x["fill:origin:unknown"] == 0.0 and x["rows:listed:shopify"] == 9


def test_the_expected_shifts_file_is_well_formed(tmp_path):
    assert all(e["pr"] for e in ph.load_expected())
    bad = tmp_path / "x.toml"
    bad.write_text('version = 1\n[[expected]]\nmetric = "fill:grams"\nchange = "sideways"\n')
    with pytest.raises(SystemExit):
        ph.load_expected(bad)


def test_judge_keeps_going_when_a_required_metric_is_missing(tmp_path, cfg):
    """A required metric not measured is a warning that says so; the other metrics are still judged, and the
    CLI writes its alerts rather than crashing."""
    results = ph.judge({"shift:rows_ratio:listed": 0.5}, cfg)
    by = {r["metric"]: r for r in results}
    assert by["fill:grams"]["status"] == "warning" and "not measured" in by["fill:grams"]["why"]
    assert by["shift:rows_ratio:listed"]["status"] == "critical"
    metrics = tmp_path / "m.json"
    metrics.write_text(json.dumps({"served": _served(), "metrics": {"shift:rows_ratio:listed": 0.5}}))
    conf = tmp_path / "cfg.toml"
    conf.write_text(CONFIG)
    out = tmp_path / "alerts.json"
    assert ph.main(["judge", str(metrics), "--config", str(conf), "--expected", str(tmp_path / "none.toml"),
                    "--out", str(out)]) == 0  # fmt: skip
    keys = {a["key"]: a for a in json.loads(out.read_text())}
    assert (
        keys["prod:grams"]["firing"]
        and keys["prod:grams"]["checks"][0]["status"] == "not measured (required)"
    )
    assert keys["prod:volume"]["firing"]
    assert "0.5" not in out.read_text()  # no measured value leaves the job


def test_a_rule_closes_only_once_its_breached_metric_is_measured_again(cfg):
    """prod:volume fired on the listed ratio; the next run has no previous release to compare with, so that metric
    isn't measured: the issue stays open. Measured and back in band, it closes."""
    gh = FakeGitHub()
    fired = ph.alerts(
        {"served": _served()}, ph.judge({"fill:grams": 0.95, "shift:rows_ratio:listed": 0.5}, cfg), "R", "s"
    )
    triage.sync(fired, gh, now="t")
    n = next(i["number"] for i in gh.store if "volume" in i["title"])
    unmeasured = ph.alerts({"served": _served()}, ph.judge({"fill:grams": 0.95}, cfg), "R", "s")
    assert triage.sync(unmeasured, gh, now="t") == []
    back = ph.alerts(
        {"served": _served()}, ph.judge({"fill:grams": 0.95, "shift:rows_ratio:listed": 1.0}, cfg), "R", "s"
    )
    assert triage.sync(back, gh, now="t") == [("closed", "prod:volume", n)]


def test_the_rules_abstain_rate_and_claudes_coverage_are_measured_apart(tmp_path):
    """abstain:enrichment is the rules' own rate (rules_complete false), whether or not Claude has reached the
    coffee; coverage:enrichment is the share of those with a Claude row. Claude working through its backlog (the
    11-09 WooCommerce jump: blocked stores reached the front of the queue) raises coverage and leaves the abstain rate
    alone, so the checked-in thresholds page on neither."""
    before = [
        _coffee(i, platform="woocommerce", complete=i >= 90) for i in range(100)
    ]  # 90 abstained, none filled
    after = [_coffee(i, platform="woocommerce", method="llm" if i < 20 else "rules", complete=i >= 90)
             for i in range(100)]  # Claude filled 20 of the 90  # fmt: skip
    b, a = (
        ph.release_metrics(_write(tmp_path / "b.parquet", before)),
        ph.release_metrics(_write(tmp_path / "a.parquet", after)),
    )
    assert b["abstain:enrichment:woocommerce"] == a["abstain:enrichment:woocommerce"] == 0.9
    assert b["coverage:enrichment:woocommerce"] == 0.0 and a["coverage:enrichment:woocommerce"] == round(
        20 / 90, 6
    )
    assert a["rows:needs_llm:woocommerce"] == 90
    cfg = ph.load_config()
    results = {r["metric"]: r["status"] for r in ph.judge(a, cfg, [], "2026-11-23")}
    assert (
        results["abstain:enrichment:woocommerce"] == "ok"
        and results["coverage:enrichment:woocommerce"] == "ok"
    )
    # the rules abstaining more pages; Claude's coverage dropping to nothing pages
    worse = [_coffee(i, platform="woocommerce", complete=False) for i in range(100)]
    results = {r["metric"]: r["status"] for r in ph.judge(ph.release_metrics(_write(tmp_path / "w.parquet", worse)),
                                                           cfg, [], "2026-11-23")}  # fmt: skip
    assert results["abstain:enrichment:woocommerce"] in ph.BREACH
    assert results["coverage:enrichment:woocommerce"] in ph.BREACH


def test_a_release_before_rules_complete_reads_the_rules_abstain_rate_from_its_values(tmp_path):
    """Without rules_complete: Claude's rows (the rules left them incomplete) plus the rules' rows that are
    incomplete by extract.needs_llm's test (no origin and not a blend, no process, or no tasting notes)."""
    cols = ("coffee_id varchar, platform varchar, is_listed boolean, origin_country varchar, process varchar, "
            "tasting_notes varchar[], is_blend boolean, enrichment_method varchar")  # fmt: skip
    rows = [
        ("r:1", "shopify", True, "Kenya", "washed", ["plum"], False, "rules"),  # complete
        (
            "r:2",
            "shopify",
            True,
            None,
            "washed",
            ["plum"],
            True,
            "rules",
        ),  # a blend needs no origin: complete
        ("r:3", "shopify", True, "Kenya", "washed", [], False, "rules"),  # no notes: abstained, not reached
        ("r:4", "shopify", True, "Kenya", "", ["plum"], False, "rules"),  # an empty process: abstained
        (
            "r:5",
            "shopify",
            True,
            "Kenya",
            "washed",
            ["plum"],
            False,
            "llm",
        ),  # Claude's: the rules had abstained
    ]
    x = ph.release_metrics(_write(tmp_path / "old.parquet", rows, cols))
    assert x["abstain:enrichment"] == 0.6 and x["rows:needs_llm"] == 3
    assert x["coverage:enrichment"] == round(1 / 3, 6)


def test_the_needs_llm_test_matches_extract():
    """The fallback SQL is extract.needs_llm's test, case by case."""
    from coffeedata import extract

    con = duckdb.connect()
    cases = [("Kenya", False, "washed", ["plum"]), (None, True, "washed", ["plum"]), (None, False, "washed", ["x"]),
             ("Kenya", False, None, ["plum"]), ("Kenya", False, "washed", []), ("", False, "washed", ["x"]),
             ("Kenya", False, "", ["x"]), (None, True, None, [])]  # fmt: skip
    for origin, blend, process, notes in cases:
        # unvalidated, so an empty string reads as the rules' own value would
        e = extract.Enrichment.model_construct(origin_country=origin, is_blend=blend, process=process,
                                               tasting_notes=notes)  # fmt: skip
        (got,) = con.execute(
            f"select {ph.RULES_INCOMPLETE} from (select ?::varchar as origin_country, ?::boolean as is_blend, "
            "?::varchar as process, ?::varchar[] as tasting_notes)",
            [origin, blend, process, notes],
        ).fetchone()
        assert got == extract.needs_llm(e), (origin, blend, process, notes)


def test_coverage_leaves_out_releases_from_before_the_enrichment_ran(tmp_path):
    """A release with no Claude row at all says nothing about Claude's backlog: its zeros stay out of every coverage
    history (they would sink the floor), and its other metrics stay in."""
    before = [_coffee(i, complete=i >= 50) for i in range(100)]  # 50 abstained, none filled yet
    after = [_coffee(i, method="llm" if i < 10 else "rules", complete=i >= 50) for i in range(100)]
    later = [_coffee(i, method="llm" if i < 20 else "rules", complete=i >= 50) for i in range(100)]
    paths = [_write(tmp_path / f"w{i}.parquet", rows) for i, rows in enumerate((before, after, later))]
    hist, text, _ = ph.baseline(paths, [], [], "2026-11-12", ["w0", "w1", "w2"])
    assert hist["coverage:enrichment"] == [0.2, 0.4] and hist["abstain:enrichment"] == [0.5, 0.5, 0.5]
    cov = next(m for m in tomllib.loads(text)["metric"] if m["name"] == "coverage:enrichment")
    assert cov["baseline"] == 0.3 and cov["warning_below"] > 0.2 * 0.75
