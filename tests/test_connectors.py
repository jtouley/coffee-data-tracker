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


def test_compact_reads_a_checkpoint_from_before_a_column_was_added(tmp_path):
    """A checkpoint written by an older image (raw 1.1.0: no fetches.partial) still compacts and merges, as null."""
    lk = Lake(str(tmp_path / "lake"))
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    run(part, WEEK, "new.com", [product("A")])
    run(part, WEEK, "old.com", [product("B")])
    old = sorted(part.part.glob("_parts/fetches/*.parquet"))[-1]
    pq.write_table(pq.read_table(old).drop(["partial"]), old)
    assert connectors.compact(part.part)["fetches"] == 2
    assert pq.read_table(part.part / "fetches.parquet").column("partial").to_pylist() == [None, None]

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


def test_egress_and_pacing_are_per_connector_config():
    assert {c.egress for c in connectors.CONNECTORS.values()} <= set(connectors.EGRESS_MODES)
    assert connectors.CONNECTORS["shopify"].egress == "dedicated"  # the confirmation run's arm
    assert all(c.egress == "default" for n, c in connectors.CONNECTORS.items() if n != "shopify")
    assert all(c.deadline_min < 240 and c.shuffle for c in connectors.CONNECTORS.values())


def test_the_confirmation_arms_split_the_roasters_into_disjoint_slices():
    ids = [f"r{i}.com" for i in range(300)]
    arms = [{r for r in ids if connectors.in_slice(r, f"{k}/3", "c1")} for k in range(3)]
    assert set().union(*arms) == set(ids) and sum(map(len, arms)) == len(ids)
    assert all(70 < len(a) < 130 for a in arms)
    assert arms[0] != {r for r in ids if connectors.in_slice(r, "0/3", "c2")}  # the salt changes the split


def test_enrich_week_never_submits_without_the_flag_even_with_keys_set(tmp_path, monkeypatch, capsys):
    """Real keys in the environment (both names) and the CLI path: nothing reaches Claude without --submit."""
    import anthropic

    from coffeedata import cli, enrich, extract

    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("COFFEEDATA_ANTHROPIC_API_KEY", "sk-test-not-real")
    assert extract.llm_enabled()
    run(lk, WEEK, "s.com", [product("A", title="Ethiopia Guji Coffee Roast")])
    control.finish(lk, control.start(lk, "merge", WEEK), "done")

    def boom(*a, **k):
        raise AssertionError("tried to reach Claude without --submit")

    monkeypatch.setattr(anthropic, "Anthropic", boom)
    monkeypatch.setattr(extract, "client", boom)
    for name in ("run", "collect", "submit"):
        if hasattr(enrich, name):
            monkeypatch.setattr(enrich, name, boom)
    cli.main(["enrich-week", "--run-date", WEEK])
    assert '"submitted": 0' in capsys.readouterr().out


def test_a_first_ever_run_on_an_empty_lake_crawls_merges_and_builds(tmp_path, monkeypatch):
    """The postmortem's gap: the weekly path with no history at all (no known product counts, no earlier run, no
    served release), through the CLI: crawl, merge, build. It must land, pass the gate and serve."""
    import httpx
    import respx

    from coffeedata import cli, http, quality

    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(connectors, "CONNECTORS", {"shopify": connectors.CONNECTORS["shopify"]})
    week = connectors.week_of(cli._today())
    stores = [Roaster(roaster_id=h, name=h, host=h, platform="shopify") for h in ("a.com", "b.com")]
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(r, "pending", None) for r in stores]
    )

    def page(req):
        p = int(req.url.params.get("page", "1"))
        host = req.url.host
        items = [{"id": i, "title": f"Ethiopia Guji {host} {i} Coffee", "handle": f"c{i}", "body_html": "Washed.",
                  "variants": [{"id": i * 10, "title": "12 oz", "price": "20.00", "grams": 340, "available": True}]}
                 for i in range(1, 4)] if p == 1 else []  # fmt: skip
        return httpx.Response(200, json={"products": items})

    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        m.get(url__regex=r"https://[^/]+/products.json").mock(side_effect=page)
        cli.main(["crawl", "--connector", "shopify", "--run-date", week])
    assert control.landed(lk, week) == {"shopify"}
    cli.main(["merge", "--run-date", week])
    con = lk.connect()
    assert (
        con.execute(
            f"select count(*) from {lk.scan('snapshots')} where run_date = ?", [date.fromisoformat(week)]
        ).fetchone()[0]
        == 6
    )
    cli.main(["build", "--db", str(tmp_path / "coffee.duckdb"), "--run-date", week])
    assert quality.served(lk) is not None  # the gate passed and the release is served
    db = duckdb.connect(str(tmp_path / "coffee.duckdb"), read_only=True)
    assert db.execute("select count(*) from dim_coffee where is_listed").fetchone()[0] == 6
    db.close()


def test_a_confirmation_arm_crawls_its_slice_into_its_own_sub_partition(tmp_path, monkeypatch):
    """The runbook's arm command: crawl --run-date --slice --salt --rps --deadline-min. Each arm writes to its own
    slice=k-of-n folder (arms run at the same time), which the weekly merge never reads."""
    import json

    import httpx
    import respx

    from coffeedata import cli, http

    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_ARM", "2-dedicated-full")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    hosts = [f"s{i}.com" for i in range(8)]
    stores = [Roaster(roaster_id=h, name=h, host=h, platform="shopify") for h in hosts]
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(r, "active", None) for r in stores]
    )
    mine = {h for h in hosts if connectors.in_slice(h, "1/3", "salt-x")}
    assert mine and mine != set(hosts)
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        m.get(url__regex=r"https://[^/]+/products.json").mock(
            side_effect=lambda req: httpx.Response(200, json={"products": []})
        )
        cli.main(["crawl", "--connector", "shopify", "--run-date", WEEK, "--slice", "1/3", "--salt", "salt-x",
                  "--rps", "5", "--deadline-min", "10"])  # fmt: skip
    part = connectors.partition(lk, "shopify", WEEK)
    assert [p.name for p in part.iterdir()] == ["slice=1-of-3"]
    got = pq.read_table(part / "slice=1-of-3" / "fetches.parquet").column("roaster_id").to_pylist()
    assert set(got) == mine
    end = [r for r in control.rows(lk, WEEK) if r["status"] != "running"]
    detail = json.loads(end[0]["detail"])
    assert (detail["slice"], detail["salt"], detail["rps"], detail["arm"]) == (
        "1/3",
        "salt-x",
        5.0,
        "2-dedicated-full",
    )
    # An arm never lands: a merge that week treats Shopify as missing instead of reading no files as its crawl.
    assert control.landed(lk, WEEK) == set()
    out = connectors.merge(lk, WEEK)
    assert not out["written"].get("fetches") and "shopify" in out["missing"]
    assert (part / "slice=1-of-3" / "fetches.parquet").exists()  # the arm's rows stay for its results


def _no_fuse_metadata_calls(monkeypatch) -> None:
    """What GCSFuse (Cloud Run's /mnt/staging) refuses: chmod, chown, utime, links and file locks."""
    import fcntl
    import os

    def refuse(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    for name in ("chmod", "fchmod", "lchmod", "chown", "fchown", "lchown", "utime", "link", "symlink"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse)
    for name in ("flock", "lockf"):
        monkeypatch.setattr(fcntl, name, refuse)


def test_lake_init_works_where_chmod_is_refused(tmp_path, monkeypatch):
    # The Cloud Run crash: ensure_schemas' chmod of its temp file on the GCSFuse mount (Errno 1).
    _no_fuse_metadata_calls(monkeypatch)
    monkeypatch.setenv("COFFEEDATA_STAGING", str(tmp_path / "staging"))
    lake = Lake("hf://datasets/someone/coffee")
    from coffeedata.lake import SCHEMA_FILES

    for table in lake.tables:
        assert (lake.root / SCHEMA_FILES[table]).exists()
    assert not list(lake.root.rglob("*.tmp"))
    # A stale schema file is refreshed too (the chmod sits on that path).
    from coffeedata.lake import SCHEMAS

    p = lake.root / SCHEMA_FILES["snapshots"]
    pq.write_table(SCHEMAS["snapshots"].remove(0).empty_table(), p)
    Lake("hf://datasets/someone/coffee")
    assert pq.read_schema(p).names == SCHEMAS["snapshots"].names and not list(p.parent.glob("*.tmp"))


def test_crawl_park_and_merge_work_without_fuse_metadata_calls(tmp_path, monkeypatch):
    # crawl, crawl-retry and merge write only through create/write/rename/unlink/rmdir, which GCSFuse supports.
    from coffeedata import ingest

    monkeypatch.setattr(
        connectors, "CONNECTORS", {k: connectors.CONNECTORS[k] for k in ("shopify", "woocommerce")}
    )
    _no_fuse_metadata_calls(monkeypatch)
    lake = _lake(tmp_path, "fuse")
    for rid, prods, adapter, hour in THIS_WEEK:
        part = connectors.PartitionLake(lake, adapter, WEEK)
        run(part, WEEK, rid, prods, hour=hour, adapter=adapter)
    shop = connectors.PartitionLake(lake, "shopify", WEEK)
    ingest.save_parked(shop, WEEK, "parked.example", {"page": 3, "items": []})
    assert ingest.load_parked(shop, WEEK) == {"parked.example": {"page": 3, "items": []}}
    for adapter in ("shopify", "woocommerce"):
        control.finish(lake, control.start(lake, "crawl", WEEK, adapter), "done")
    out = connectors.merge(lake, WEEK)
    assert out["missing"] == [] and out["written"]["fetches"] == 3
    assert not list(lake.root.rglob("*.tmp"))


def test_arm_rows_never_land_or_feed_the_egress_flag(tmp_path, monkeypatch):
    """Arms from any image (detail.slice or detail.arm, job still `crawl`) are experiments: they don't make a
    connector landed for the week, and their refusals never pair with a production run in egress-check."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "34.1.2.3")
    for detail in ({"slice": "2/3", "arm": "3-dedicated-low"}, {"slice": None, "arm": "1-default-full"}):
        job = control.start(lk, "crawl", WEEK, "shopify")
        control.finish(lk, job, "done", first_contact={"stores": 100, "refused": 50}, detail=detail)
    assert control.landed(lk, WEEK) == set()
    job = control.start(lk, "crawl", "2026-11-09", "shopify")  # the first production run after cutover
    control.finish(
        lk, job, "done", first_contact={"stores": 100, "refused": 9}, detail={"slice": None, "arm": None}
    )
    assert control.landed(lk, "2026-11-09") == {"shopify"}
    assert control.egress_flags(lk) == []  # one production run above 8% isn't two
    job = control.start(lk, "crawl", "2026-11-16", "shopify")
    control.finish(lk, job, "done", first_contact={"stores": 100, "refused": 10})
    assert control.egress_flags(lk) == [{"ip": "34.1.2.3", "shares": [0.09, 0.1]}]


def test_roasters_on_one_host_are_always_in_the_same_arm(tmp_path, monkeypatch):
    """The split hashes the host: two roaster_ids served by one store never end up in two arms."""
    lk = Lake(str(tmp_path / "lake"))
    stores = [Roaster(roaster_id=f"d{i}.com", name="r", host=f"shop{i % 10}.example", platform="shopify")
              for i in range(40)]  # fmt: skip
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(r, "active", None) for r in stores]
    )
    seen = []
    monkeypatch.setattr(connectors.ingest, "run", lambda plake, states, *a, **k: seen.append(states) or
                        {"roasters": 0, "by_adapter": {}, "skipped_deadline": 0, "deferred": {}})  # fmt: skip
    monkeypatch.setattr(lk, "publish", lambda *a, **k: None)
    for k in range(3):
        connectors.crawl(lk, "shopify", WEEK, part=f"{k}/3", salt="s")
    arms = [{s.roaster.host for s in states} for states in seen]
    assert sum(len(a) for a in arms) == 10 and set().union(*arms) == {f"shop{i}.example" for i in range(10)}
    assert all(len(states) == 4 * len(a) for states, a in zip(seen, arms, strict=True))  # all 4 per host


def test_compact_is_idempotent_after_a_kill_between_write_and_unlink(tmp_path):
    """Killed after the compacted file was written but before its parts were removed: the next compact (a resume)
    keeps each row once."""
    import shutil

    lk = Lake(str(tmp_path / "lake"))
    part = connectors.PartitionLake(lk, "shopify", WEEK)
    run(part, WEEK, "a.com", [product("A"), product("B")])
    connectors.compact(part.part)
    run(part, WEEK, "b.com", [product("C")])
    saved = tmp_path / "saved"
    shutil.copytree(part.part / "_parts", saved)
    first = connectors.compact(part.part)
    shutil.copytree(saved, part.part / "_parts")  # as if the unlinks never happened
    assert connectors.compact(part.part) == first
    assert first["fetches"] == 2 and first["snapshots"] == 3
    got = pq.read_table(part.part / "snapshots.parquet").column("product_id").to_pylist()
    assert sorted(got) == ["A", "B", "C"]


def test_a_crawl_stopped_by_its_deadline_exits_0(tmp_path, monkeypatch, capsys):
    """Cloud Run retries a non-zero exit, and the retry would re-ask the stores that already refused. The deadline
    is reported in job_manifests (status deadline) and on stderr instead."""
    from coffeedata import cli

    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(connectors, "crawl", lambda *a, **k: {"status": "deadline", "skipped_deadline": 3})
    cli.main(["crawl", "--connector", "shopify", "--run-date", WEEK])  # no SystemExit
    assert "deadline reached, 3 roasters left" in capsys.readouterr().err


def test_a_main_pass_and_its_resume_are_one_run_for_egress_check(tmp_path, monkeypatch):
    """R2: the 15:17 resume slot is a second `crawl` row of the same week. Its stores are the main pass's leftovers,
    mostly refusers, so on its own its share is far above 8%. With the main pass above 8% too, the last two rows used
    to be one week's pass and its resume: a false alarm. A run is a run_date, so one bad week is never two runs."""
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "34.1.2.3")
    control.finish(lk, control.start(lk, "crawl", "2026-11-09", "shopify"), "done",
                   first_contact={"stores": 400, "refused": 36})  # fmt: skip
    # a resume row as an image before this fix wrote it: its leftovers counted again as first contacts
    control.finish(lk, control.start(lk, "crawl", "2026-11-09", "shopify"), "done",
                   first_contact={"stores": 40, "refused": 30})  # fmt: skip
    # and as this fix writes it: only stores never tried this week are first contacts
    control.finish(lk, control.start(lk, "crawl", "2026-11-09", "shopify"), "done",
                   first_contact={"stores": 0, "refused": 0})  # fmt: skip
    assert control.egress_flags(lk) == []


def test_two_bad_weeks_still_flag_with_their_resumes_folded_in(tmp_path, monkeypatch):
    lk = Lake(str(tmp_path / "lake"))
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "34.1.2.3")
    for week, main, resume in (("2026-11-09", (400, 36), (5, 2)), ("2026-11-16", (400, 40), (0, 0))):
        for stores, refused in (main, resume):
            control.finish(lk, control.start(lk, "crawl", week, "shopify"), "done",
                           first_contact={"stores": stores, "refused": refused})  # fmt: skip
    assert control.egress_flags(lk) == [{"ip": "34.1.2.3", "shares": [0.094, 0.1]}]
