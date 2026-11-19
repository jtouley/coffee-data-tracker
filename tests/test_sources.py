"""The discovery source list (lake `candidates`) and the owner's flags on it: discovery records every place it reads
and fetches none of them; only places in the registry, and places a published snapshot flags as roasters, are ever
probed or crawled (registry.current), and the build's stg_registry agrees."""

import argparse
import json
from datetime import UTC, datetime

import duckdb
import pytest

from coffeedata import cli, decisions, discover, ingest, quality, registry
from coffeedata.lake import Lake

from .lakehelp import product, run
from .test_flags import _serve_snapshot

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def place(d, name, tags=("cafe",), roast=False, at=NOW, source="overture-2026-10"):
    return {"added_at": at, "source": source, "roaster_id": d, "name": name, "city": "Portland", "state": "OR",
            "website": f"https://{d}/", "locations": 1, "confidence": 0.9, "tags": list(tags),
            "roast_in_name": roast}  # fmt: skip


def test_discovery_records_every_place_and_keeps_roasters_as_before():
    rows = [  # website, name, city, state, confidence, tagged, roast_in_name, is_roaster, tag, open
        ("https://www.rosterco.com/", "Roster Co", "Bend", "OR", 0.9, True, False, True, None, True),
        ("https://cafe.example/menu", "Cafe One", "Bend", "OR", 0.8, False, False, False, "cafe", True),
        (
            "https://cafe.example/",
            "Cafe One (2)",
            "Bend",
            "OR",
            0.5,
            False,
            False,
            False,
            "coffee_shop",
            True,
        ),
        ("https://closed.example/", "Closed", "Bend", "OR", 0.9, False, False, False, "cafe", False),
        ("https://facebook.com/x", "Social", "Bend", "OR", 0.9, False, False, False, "cafe", True),
        (
            "https://rival.example/",
            "Rival Roasters",
            "Bend",
            "OR",
            0.7,
            False,
            True,
            True,
            "coffee_shop",
            True,
        ),
    ]
    roasters, places = discover.places_to_rows(rows, "overture-x")
    assert [r["roaster_id"] for r in roasters] == ["rival.example", "rosterco.com"]
    assert "website" not in roasters[0] and roasters[1]["tagged_roastery"]
    by = {p["roaster_id"]: p for p in places}
    assert set(by) == {
        "rosterco.com",
        "cafe.example",
        "rival.example",
    }  # closed cafés and social sites are left out
    assert by["cafe.example"]["tags"] == ["cafe", "coffee_shop"] and by["cafe.example"]["locations"] == 2
    assert by["rosterco.com"]["tags"] == ["coffee_roastery"] and by["rival.example"]["roast_in_name"]


@pytest.fixture
def sources_lake(lake, monkeypatch):
    """Two cafés in the source list (no site of theirs is ever asked), one roaster from the seed."""
    lake.write("candidates", "candidates/source=overture/a.parquet",
               [place("beanhouse.example", "Bean House Roasters", roast=True), place("teashop.example", "Tea Shop")])  # fmt: skip
    monkeypatch.setattr(registry, "SEED", lake.root / "seed.csv")
    (lake.root / "seed.csv").write_text(",".join(registry.SEED_FIELDS) + "\n" + "seeded.example,Seeded"
                                        + "," * (len(registry.SEED_FIELDS) - 2) + "\n")  # fmt: skip
    return lake


def _ids(lake):
    return sorted(s.roaster.roaster_id for s in registry.current(lake, lake.root / "seed.csv"))


def test_only_a_flagged_place_reaches_the_registry_the_probe_and_the_crawl(sources_lake):
    lake = sources_lake
    assert _ids(lake) == ["seeded.example"]  # nothing flagged: the source list adds nothing
    doc = decisions.snapshot("s1", "t", [], sources=["beanhouse.example"])
    _serve_snapshot(lake, "b1", doc)
    assert _ids(lake) == [
        "beanhouse.example",
        "seeded.example",
    ]  # flagged roaster: in, as discovery would add it
    st = {s.roaster.roaster_id: s for s in registry.current(lake, lake.root / "seed.csv")}
    assert (
        st["beanhouse.example"].roaster.name == "Bean House Roasters"
        and st["beanhouse.example"].last_probed is None
    )
    # the probe asks it (never probed), and once a probe finds its storefront the weekly crawl selects it
    lake.write_partition("probes", "2026-11-02", "p1", [{"run_date": NOW.date(), "batch_id": "p1", "probed_at": NOW,
                                                          "roaster_id": "beanhouse.example", "status": "ok",
                                                          "final_host": "beanhouse.example", "platform": "shopify"}])  # fmt: skip
    crawled = [s.roaster.roaster_id for s in ingest.select_states(lake, "2026-11-02")[0]]
    assert "beanhouse.example" in crawled and "teashop.example" not in crawled


def test_the_registry_and_the_crawl_are_unchanged_until_flagged(sources_lake):
    """Every registry roaster inherits the roaster flag: with the source list in the lake and no flag, the registry
    and the crawl's selection are exactly what they are without it (main's)."""
    lake = sources_lake
    probe = {"run_date": NOW.date(), "batch_id": "p1", "probed_at": NOW, "roaster_id": "seeded.example",
             "status": "ok", "final_host": "seeded.example", "platform": "shopify"}  # fmt: skip
    lake.write_partition("probes", "2026-11-02", "p1", [probe])
    with_list = (registry.current(lake, lake.root / "seed.csv"), ingest.select_states(lake, "2026-11-02"))
    for f in (lake.root / "candidates").rglob("*.parquet"):  # main: no source list at all
        if "schema" not in f.name:
            f.unlink()
    assert (
        registry.current(lake, lake.root / "seed.csv"),
        ingest.select_states(lake, "2026-11-02"),
    ) == with_list
    assert [s.roaster.roaster_id for s in with_list[1][0]] == ["seeded.example"]


def test_pull_now_crawls_a_flagged_place_before_it_is_published(sources_lake):
    """`crawl --only` (and `probe --only`) take domains, comma-separated; one that only the source list has (flagged,
    no draft published yet) is crawled all the same, and nothing else of the source list is."""
    lake = sources_lake
    probe = {"run_date": NOW.date(), "batch_id": "p1", "probed_at": NOW, "roaster_id": "beanhouse.example",
             "status": "ok", "final_host": "beanhouse.example", "platform": "woocommerce"}  # fmt: skip
    lake.write_partition("probes", "2026-11-02", "p1", [probe])
    states, mode = ingest.select_states(lake, "2026-11-02", only="beanhouse.example,seeded.example")
    assert mode == "only" and sorted(s.roaster.roaster_id for s in states) == [
        "beanhouse.example",
        "seeded.example",
    ]
    assert {s.roaster.roaster_id: s.roaster.adapter for s in states}["beanhouse.example"] == "woocommerce"
    assert _ids(lake) == ["seeded.example"]  # the registry itself is unchanged until a draft is published


def test_pull_probes_then_crawls_each_storefront_on_its_connector(sources_lake, monkeypatch):
    """`coffeedata pull`: the probe, then a counted crawl of each place that answered with a storefront, on its
    platform's connector, into the week asked; a place with no storefront is never crawled."""
    calls = []
    monkeypatch.setattr(discover, "probe", lambda places, workers, run_date: [
        {"run_date": NOW.date(), "batch_id": "x", "probed_at": NOW, "roaster_id": d, "status": "ok",
         "final_host": d, "platform": {"beanhouse.example": "woocommerce", "teashop.example": None}[d]}
        for d in places])  # fmt: skip
    monkeypatch.setattr(cli, "Lake", lambda: sources_lake)
    monkeypatch.setattr(
        "coffeedata.connectors.crawl", lambda lake, c, day, **k: calls.append((c, day, k)) or {}
    )
    cli.cmd_pull(argparse.Namespace(only="beanhouse.example,teashop.example", run_date="2026-11-09"))
    assert calls == [("woocommerce", "2026-11-09", {"only": "beanhouse.example"})]


def test_a_registry_roaster_marked_not_a_roaster_leaves_the_crawl(sources_lake):
    _serve_snapshot(sources_lake, "b1", decisions.snapshot("s1", "t", [], not_roasters=["seeded.example"]))
    assert _ids(sources_lake) == []


def test_an_unreadable_snapshot_adds_no_place(sources_lake, monkeypatch):
    monkeypatch.setattr(quality, "served", lambda _lake: (_ for _ in ()).throw(OSError("429")))
    assert _ids(sources_lake) == ["seeded.example"]


def test_the_snapshot_carries_the_flagged_places():
    doc = decisions.snapshot("s1", "t", [], sources=["b.example", "a.example", "a.example"])
    assert doc["sources"] == ["a.example", "b.example"] and decisions.source_roasters(doc) == doc["sources"]
    assert "sources" not in decisions.snapshot("s2", "t", [])  # none: the document #128 wrote
    with pytest.raises(decisions.SnapshotError, match="sources"):
        decisions.check(doc | {"sources": [1]})


@pytest.mark.skipif(__import__("shutil").which("dbt") is None, reason="dbt not installed")
def test_the_build_registers_a_flagged_place_like_the_crawl(lake, monkeypatch, tmp_path):
    """A draft whose snapshot flags a place: stg_registry (and so dim_roaster) has it, the same rule as
    registry.current; one that isn't flagged stays out, and a roaster flagged not a roaster leaves the build."""
    real = cli.dbt_build
    monkeypatch.setattr(cli, "dbt_build", lambda lk, db, extra=None: real(
        lk, db, (extra or {}) | {"min_attempted_share": 0, "min_ok_share": 0, "min_adapter_ok_share": 0}))  # fmt: skip
    monkeypatch.setattr(
        cli, "MARTS", ["dim_coffee", "dim_roaster", "mart_market", "fct_price_event", "fct_shot"]
    )
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    monkeypatch.setattr(cli, "_today", lambda: "2026-10-19")
    snaps = tmp_path / "snapshots"
    monkeypatch.setenv("COFFEEDATA_SNAPSHOTS", str(snaps))
    lake.write("candidates", "candidates/source=overture/a.parquet",
               [place("beanhouse.example", "Bean House"), place("teashop.example", "Tea Shop"),
                place("osmtoo.example", "Osm Too Open")])  # fmt: skip
    # A flagged place OpenStreetMap already added: its Overture record still counts (only the same roaster and
    # source is skipped), so it is published with its open name, never the OSM one.
    lake.write("registry", "registry/source=osm/x.parquet",
               [{"added_at": NOW, "source": "osm", "roaster_id": "osmtoo.example", "name": "Osm Too ODbL"}])  # fmt: skip
    run(lake, "2026-10-19", "beanhouse.example", [product(str(i)) for i in range(30)])
    run(lake, "2026-10-19", "osmtoo.example", [product(str(i)) for i in range(5)])
    dropped = "107roasters.com"  # a seed roaster the owner flagged not a roaster
    doc = decisions.snapshot(
        "s1", "t", [], sources=["beanhouse.example", "osmtoo.example"], not_roasters=[dropped]
    )
    (snaps / "snapshots").mkdir(parents=True)
    (snaps / decisions.key("s1")).write_text(json.dumps(doc, sort_keys=True))
    args = argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date="2026-10-19", accept_volume_change=False,
                              draft="d1", snapshot="s1", snapshot_sha256=decisions.digest(doc))  # fmt: skip
    cli.cmd_build(args)
    draft = next(b for b, d in quality.drafts(lake).items() if d == "d1")
    f = quality.release_files(Lake(), draft)["dim_roaster"]
    rows = dict(duckdb.connect().execute(f"select roaster_id, sources from '{f}'").fetchall())
    assert rows.get("beanhouse.example") == "overture-2026-10" and "teashop.example" not in rows
    assert dropped not in rows and len(rows) > 1000  # not a roaster: out of the build too, the rest kept
    assert "overture-2026-10" in rows["osmtoo.example"]
    pub = quality.release_files(Lake(), draft)["public_roaster"]
    names = dict(duckdb.connect().execute(f"select roaster_id, roaster_name from '{pub}'").fetchall())
    assert names.get("osmtoo.example") == "Osm Too Open"
