"""Design B: per-connector partitions, compaction and the weekly merge hand dbt the same run as one ingest would."""

from datetime import date

import duckdb
import pyarrow.parquet as pq

from coffeedata import connectors, control, registry
from coffeedata.lake import Lake
from coffeedata.models import Roaster

from .lakehelp import product, run
from .test_dbt import build

WEEK = "2026-11-02"
HISTORY = [
    ("2026-10-26", "onyxcoffeelab.com", [product("A", 20.0), product("B", 18.0)], "shopify"),
    ("2026-10-26", "anaheimcoffee.com", [product("W", 15.0)], "woocommerce"),
]
THIS_WEEK = [
    ("onyxcoffeelab.com", [product("A", 22.0), product("C", 25.0)], "shopify", 3),
    ("anaheimcoffee.com", None, "woocommerce", 2),  # failed on the first slot
    ("anaheimcoffee.com", [product("W", 16.0)], "woocommerce", 8),
]  # its resume slot fixed it


def _lake(tmp_path, name) -> Lake:
    lk = Lake(str(tmp_path / name))
    for d, rid, prods, adapter in HISTORY:
        run(lk, d, rid, prods, adapter=adapter)
    return lk


def _marts(con) -> dict:
    return {
        "events": sorted(
            con.execute(
                "select run_date, coffee_id, event, price_usd, previous_price_usd from fct_price_event"
            ).fetchall()
        ),
        "coffees": sorted(
            con.execute(
                "select coffee_id, is_listed, in_stock, first_seen, last_seen from dim_coffee"
            ).fetchall()
        ),
        "complete": sorted(
            con.execute(
                "select run_date, roaster_id, attempted, ok, adapter, catalog_ok from run_completeness"
            ).fetchall()
        ),
    }


def test_merged_partitions_build_the_same_marts_as_one_ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(
        connectors, "CONNECTORS", {k: connectors.CONNECTORS[k] for k in ("shopify", "woocommerce")}
    )
    direct = _lake(tmp_path, "direct")
    for rid, prods, adapter, hour in THIS_WEEK:
        run(direct, WEEK, rid, prods, hour=hour, adapter=adapter)
    merged = _lake(tmp_path, "merged")
    for rid, prods, adapter, hour in THIS_WEEK:
        part = connectors.PartitionLake(merged, adapter, "2026-11-04")
        run(part, WEEK, rid, prods, hour=hour, adapter=adapter)
    for adapter in ("shopify", "woocommerce"):  # each connector job's control rows: started, then done
        control.finish(merged, control.start(merged, "crawl", WEEK, adapter), "done")
    # The partitions are read back like the lake (resume), and dbt's globs don't see them before the merge.
    part = connectors.PartitionLake(merged, "woocommerce", WEEK)
    assert (
        part.connect()
        .execute(
            f"select count(*) from {part.scan('fetches')} where run_date = ?", [date.fromisoformat(WEEK)]
        )
        .fetchone()[0]
        == 2
    )
    assert not list((merged.root / "fetches").glob(f"run_date={WEEK}/*"))
    out = connectors.merge(merged, "2026-11-08")
    assert out["missing"] == [] and out["written"]["fetches"] == 3
    # One file per table for the week, the raw partitions gone, the manifests rolled up into one.
    assert len(list((merged.root / "fetches").glob(f"run_date={WEEK}/*.parquet"))) == 1
    assert len(list((merged.root / "snapshots").glob(f"run_date={WEEK}/*.parquet"))) == 1
    assert not list((merged.root / "raw").glob("connector=*"))
    jobs = control.rows(merged, WEEK)  # the control plane: what ran, rolled into one file for the week
    assert sorted((j["job"], j["status"]) for j in jobs) == [("crawl", "done")] * 2 + [("merge", "done")]
    assert len(list((merged.root / "job_manifests").glob(f"run_date={WEEK}/*.parquet"))) == 1
    # Merging again adds nothing (no duplicate rows).
    assert connectors.merge(merged, WEEK)["written"] == {}
    a, b = _marts(build(direct, tmp_path / "a")), _marts(build(merged, tmp_path / "b"))
    assert a == b and a["events"]


def test_a_connector_that_never_ran_stays_in_the_expected_set(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    states = [
        registry.RoasterState(Roaster(roaster_id=r, name=r, host=r, platform=p), "active", None)
        for r, p in (("s.com", "shopify"), ("w.com", "woocommerce"), ("g.com", "wix"))
    ]
    monkeypatch.setattr(registry, "current", lambda lake, seed=None: states)
    run(connectors.PartitionLake(lk, "shopify", WEEK), WEEK, "s.com", [product("A")])
    control.finish(lk, control.start(lk, "crawl", WEEK, "shopify"), "done")
    # woocommerce's job started but never finished (killed): it hasn't landed, so it counts as missing
    control.start(lk, "crawl", WEEK, "woocommerce")
    out = connectors.merge(lk, WEEK)
    assert "woocommerce" in out["missing"] and "generic" in out["missing"]
    rows = (
        duckdb.connect()
        .execute(f"select roaster_id, mode from {lk.scan('run_manifests')} where roaster_id is not null")
        .fetchall()
    )
    assert sorted(rows) == [("g.com", "full"), ("w.com", "full")]


def test_compact_folds_checkpoints_into_one_file(tmp_path):
    lk = Lake(str(tmp_path / "lake"))
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    for i in range(3):
        run(part, WEEK, f"r{i}.com", [product("A")])
    assert len(list(part.part.glob("_parts/fetches/*.parquet"))) == 3
    assert connectors.compact(part.part)["fetches"] == 3
    assert sorted(p.name for p in part.part.iterdir()) == ["fetches.parquet", "snapshots.parquet"]
    assert pq.read_metadata(part.part / "fetches.parquet").num_rows == 3


def test_week_of():
    assert connectors.week_of("2026-11-08") == connectors.week_of("2026-11-02") == "2026-11-02"


def test_past_the_deadline_roasters_are_left_unattempted(lake):
    from coffeedata import ingest

    roasters = [
        Roaster(roaster_id=f"r{i}.com", name="r", host=f"r{i}.com", platform="shopify") for i in range(3)
    ]
    out = ingest.run(lake, roasters, WEEK, workers=1, deadline_s=0)
    assert out["skipped_deadline"] == 3 and out["by_adapter"] == {}
    # Unattempted means no fetches row, so a resume picks them up and the run doesn't count them as tried.
    con = lake.connect()
    assert con.execute(f"select count(*) from {lake.scan('fetches')}").fetchone()[0] == 0
    assert (
        con.execute(
            f"select count(*) from {lake.scan('run_manifests')} where roaster_id is not null"
        ).fetchone()[0]
        == 3
    )


def test_enrichment_candidates_come_from_raw_with_the_builds_rules(tmp_path, monkeypatch):
    """ELT: the crawl writes no enrichment rows; the post-load job decides from raw snapshots and bodies as
    stg_products does (a coffee whose rule attributes are incomplete), and waits for the week's merge."""
    from coffeedata import enrich, extract

    monkeypatch.setattr(extract, "llm_enabled", lambda: False)
    lk = Lake(str(tmp_path / "lake"))
    bare = product("A", title="Ethiopia Guji Coffee Roast")  # no origin details in the body: incomplete
    run(lk, WEEK, "s.com", [bare, product("M", coffee=False, title="Logo Mug")])
    lk.write("bodies", f"bodies/run_date={WEEK}/batch=b1.parquet",
             [{"body_hash": "bA", "body_text": "A lovely cup.", "first_seen_run": date.fromisoformat(WEEK)}])  # fmt: skip
    got = enrich.candidates(lk)
    assert [(c["roaster_id"], c["product_id"]) for c in got] == [("s.com", "A")]  # not the mug
    assert got[0]["base"].method == "rules"
    assert connectors.enrich_week(lk, WEEK)["status"].startswith("waiting")  # no merge yet: control table
    control.finish(lk, control.start(lk, "merge", WEEK), "done")
    out = connectors.enrich_week(lk, WEEK)
    assert out["candidates"] == 1 and out["submitted"] == 0  # without --submit: counted, never sent
    called = []
    monkeypatch.setattr(extract, "llm_enabled", lambda: True)
    monkeypatch.setattr(enrich, "run", lambda *a, **k: called.append(1) or {})
    monkeypatch.setattr(enrich, "collect", lambda *a, **k: {})
    connectors.enrich_week(lk, WEEK)
    assert called == []  # credentials alone never spend
    connectors.enrich_week(lk, WEEK, submit=True)
    assert called == [1]
    assert sorted(j["job"] for j in control.rows(lk, WEEK)) == ["enrich"] * 3 + ["merge"]


def test_egress_ip_refused_on_over_8_percent_two_runs_in_a_row_is_flagged(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "34.1.2.3")
    for week, refused in (("2026-10-26", 9), ("2026-11-02", 12)):
        job = control.start(lk, "crawl", week, "shopify")
        control.finish(lk, job, "done", first_contact={"stores": 100, "refused": refused})
    assert control.egress_flags(lk) == [{"ip": "34.1.2.3", "shares": [0.09, 0.12]}]
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "34.9.9.9")
    job = control.start(lk, "crawl", "2026-11-09", "shopify")
    control.finish(lk, job, "done", first_contact={"stores": 100, "refused": 20})
    assert [f["ip"] for f in control.egress_flags(lk)] == ["34.1.2.3"]  # one bad run isn't a trend
