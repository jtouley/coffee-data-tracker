"""The owner's chain flags (docs/admin/DESIGN.md, Sources): a registry roaster flagged a chain or not a chain, over
its store count, either way, with a reason, from the Sources page's row menu. Recorded in the control plane
(control.chain_flags, seeded with dbt/seeds/chain_overrides.csv), audited and idempotent, carried by the next draft's
snapshot (`chains`), and passed to dbt as var chain_flags by `coffeedata build`."""

import argparse
import csv
import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from admin.sources import CHAIN_STORES
from coffeedata import cli, decisions

from .test_admin import B1, W1, build_lake
from .test_admin_actions import audit, make, post
from .test_admin_sources import RUN2
from .test_sources import place

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "dbt" / "seeds" / "chain_overrides.csv"


@pytest.fixture
def lake(tmp_path):
    """The served release's registry: two curated chains the seed flags (one with a low count, one with 21), a chain
    by its count alone, and an independent roaster; one café in the source list."""
    lake = build_lake(tmp_path / "lake")
    from coffeedata.lake import Lake

    Lake(str(lake.root)).write("candidates", "candidates/source=overture/r1.parquet",
                               [place("cafe.example", "Corner Cafe", tags=["coffee_shop"], at=RUN2)])  # fmt: skip
    rel = lake.root / "releases" / f"run_date={W1}" / f"build={B1}" / "dim_roaster.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"roaster_id": "peets.com", "roaster_name": "Peet's Coffee", "city": "Emeryville", "state": "CA",
         "sources": "curated", "locations": 1, "coffees_listed": 105},
        {"roaster_id": "coffeebean.com", "roaster_name": "The Coffee Bean & Tea Leaf", "city": "Los Angeles",
         "state": "CA", "sources": "overture-2026-10", "locations": 21, "coffees_listed": 43},
        {"roaster_id": "farmerbros.com", "roaster_name": "Farmer Brothers", "city": "Northlake", "state": "TX",
         "sources": "overture-2026-10", "locations": 62, "coffees_listed": 0},
        {"roaster_id": "lacolombe.com", "roaster_name": "La Colombe", "city": "Philadelphia", "state": "PA",
         "sources": "curated+overture-2026-10", "locations": 13, "coffees_listed": 25},
    ]), rel)  # fmt: skip
    return lake


def _text(page: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def test_the_migration_seeds_the_flags_with_the_seed_csv(tmp_path, lake, pg_url):
    """control.chain_flags starts with dbt's seed, row for row, so the first draft carrying chain flags builds what
    the seed built."""
    app, _, _ = make(tmp_path, lake.root, pg_url)
    with SEED.open(newline="") as f:
        want = {
            r["roaster_id"]: ("chain" if r["is_chain"] == "true" else "not_chain", r["reason"])
            for r in csv.DictReader(f)
        }
    app.state.writes.control.migrate()
    got = app.state.writes.control.chain_flags()
    assert {r: (v["flag"], v["reason"]) for r, v in got.items()} == want
    assert {v["actor"] for v in got.values()} == {"seed"}


def test_the_threshold_is_dbts():
    """The page's count rule is dbt's: more than chain_store_threshold stores (dbt_project.yml, from #146; the page
    reads 20 until the stack carries it)."""
    project = (ROOT / "dbt" / "dbt_project.yml").read_text()
    m = re.search(r"^\s*chain_store_threshold:\s*(\d+)", project, re.M)
    assert (int(m.group(1)) if m else 20) == CHAIN_STORES


def test_sources_shows_each_chain_and_why(tmp_path, lake, pg_url):
    _, c, _ = make(tmp_path, lake.root, pg_url)
    page = c.get("/part/sources").text
    text = _text(page)
    assert "3 chains, crawled but not public" in text  # peets and coffeebean flagged, farmerbros by its count
    assert text.count("· Chain") == 6  # each chain's word, on a desktop and in a phone's line
    assert "A chain: 62 stores counted, more than 20." in text
    assert "Not a chain: 13 stores counted, 20 or fewer." in text
    assert "A chain, flagged by hand (1 store counted): Peet" in text
    assert "flagged a chain (2)" in text and "over 20 stores (1)" in text  # the facet's values
    assert "(none)" not in page.split('name="chain"')[1].split("</select>")[0]
    flagged = _text(c.get("/part/sources?chain=flagged+a+chain").text)
    assert "2 of 5 places" in flagged and "La Colombe" not in flagged and "chain: flagged a chain" in flagged
    # each status offers the other way, and a flag can be cleared
    assert "Mark as a chain…" in page and "Not a chain…" in page and "Clear the chain flag…" in page
    assert "Mark La Colombe as a chain?" in page and "Mark Farmer Brothers as not a chain?" in page
    assert "Its 25 listed coffees leave the public data" in page
    # peets.com: flagged, and its count (1) says not a chain, so Clear is its one way back (no second, redundant flag)
    peets = page[page.index("Actions for Peet") :].split("</details>")[0]
    assert "Clear the chain flag on Peet" in peets and "Not a chain…" not in peets
    # coffeebean.com: flagged, and its count (21) agrees, so Not a chain, and Clear (the count decides from then on)
    bean = page[page.index("Actions for The Coffee Bean") :].split("</details>")[0]
    assert "Not a chain…" in bean and "Clear the chain flag…" in bean
    assert (
        "Cafe" in text and text.count("Mark as a chain…") == 1
    )  # a café of the source list has no chain menu


def test_a_chain_flag_is_reasoned_idempotent_conflict_checked_and_audited(tmp_path, lake, pg_url):
    app, c, _ = make(tmp_path, lake.root, pg_url)
    url = "/api/roasters/farmerbros.com/chain"
    assert post(c, url, {"key": "k0", "chain": "not_chain", "reason": ""}).status_code == 400
    assert post(c, url, {"key": "k1", "chain": "maybe", "reason": "why not"}).status_code == 400
    body = {
        "key": "k2",
        "chain": "not_chain",
        "seen": "none",
        "reason": "a distributor: branches, not stores",
    }
    r1, r2 = post(c, url, body), post(c, url, body)
    assert r1.status_code == 200 and r1.json() == r2.json() == {"ok": True, "roaster_id": "farmerbros.com",
                                                                "chain": "not_chain"}  # fmt: skip
    stale = post(c, url, body | {"key": "k3", "chain": "chain"})
    assert (
        stale.status_code == 409 and "changed after this page loaded" in stale.json()["detail"]
    )  # another tab flagged it
    again = post(c, url, body | {"key": "k4", "seen": "not_chain"})
    assert again.status_code == 400 and "already" in again.json()["detail"]
    assert post(c, "/api/roasters/x%2F..%2Fy/chain", body | {"key": "k5"}).status_code in (400, 404)
    rows = [a for a in audit(app) if a["action"] == "chain_flag"]
    assert [a["subject"] for a in rows] == ["farmerbros.com"]
    page = _text(c.get("/part/sources?q=farmer").text)
    assert (
        "· Not a chain" in page and "Not a chain, flagged by hand (62 stores counted): a distributor" in page
    )
    cleared = post(c, url, {"key": "k6", "chain": "clear", "seen": "not_chain", "reason": "counted again"})
    assert cleared.status_code == 200 and "A chain: 62 stores counted" in _text(
        c.get("/part/sources?q=farmer").text
    )
    nothing = post(c, url, {"key": "k7", "chain": "clear", "seen": "none", "reason": "nothing to clear"})
    assert nothing.status_code == 400


def test_the_next_draft_carries_the_chain_flags_and_release_says_so(tmp_path, lake, pg_url):
    _, c, _ = make(tmp_path, lake.root, pg_url)
    release = _text(c.get("/part/release").text)
    assert "4 chain flags not in it" in release  # the served snapshot is from before chain flags
    assert "peets.com becomes a chain" in release
    post(c, "/api/roasters/farmerbros.com/chain",
         {"key": "k1", "chain": "not_chain", "seen": "none", "reason": "a distributor"})  # fmt: skip
    post(
        c, "/api/roasters/peets.com/chain", {"key": "k2", "chain": "clear", "seen": "chain", "reason": "test"}
    )
    release = _text(c.get("/part/release").text)
    assert "4 chain flags not in it" in release and "farmerbros.com becomes not a chain" in release
    d = post(c, "/api/drafts", {"key": "k3"}).json()
    doc = json.loads((tmp_path / "snapshots" / decisions.key(d["snapshot_id"])).read_text())
    assert decisions.chain_flags(doc) == [
        {"roaster_id": "cariboucoffee.com", "chain": True}, {"roaster_id": "cleaneatz.com", "chain": True},
        {"roaster_id": "coffeebean.com", "chain": True}, {"roaster_id": "farmerbros.com", "chain": False}]  # fmt: skip


def test_without_the_control_plane_no_chain_is_shown(tmp_path, lake):
    from admin.controlplane import ControlPlane, url_connect

    dead = ControlPlane(url_connect("postgresql://nobody@127.0.0.1:1/none"))
    _, c, _ = make(tmp_path, lake.root, "postgresql://nobody@127.0.0.1:1/none", control_plane=dead)
    page = c.get("/part/sources?chain=flagged+a+chain").text
    assert (
        "Peet" in page
        and "· Chain" not in page
        and "chains, crawled" not in page
        and 'name="chain"' not in page
    )


def test_the_snapshot_carries_chains_only_when_given():
    doc = decisions.snapshot("s1", "t", [], chains=[{"roaster_id": "b.example", "chain": False},
                                                     {"roaster_id": "a.example", "chain": 1}])  # fmt: skip
    assert doc["chains"] == [
        {"roaster_id": "a.example", "chain": True},
        {"roaster_id": "b.example", "chain": False},
    ]
    assert (
        decisions.chain_flags(decisions.snapshot("s2", "t", [], chains=[])) == []
    )  # no override: not the seed
    assert "chains" not in decisions.snapshot("s3", "t", []) and decisions.chain_flags(None) is None
    for bad in ([{"roaster_id": "a.example"}], [{"roaster_id": "a.example", "chain": "yes"}],
                [{"roaster_id": "a.example", "chain": True}] * 2):  # fmt: skip
        with pytest.raises(decisions.SnapshotError, match="chains"):
            decisions.check(doc | {"chains": bad})


@pytest.mark.parametrize("chains", [None, [{"roaster_id": "peets.com", "chain": False}]])
def test_the_build_passes_the_snapshots_chain_flags_to_dbt(chains, monkeypatch, tmp_path):
    """dbt reads var chain_flags in place of its seed only when the snapshot carries chain flags."""
    from coffeedata.lake import Lake

    lake = Lake(str(tmp_path / "lake"))
    snaps = tmp_path / "snapshots"
    monkeypatch.setenv("COFFEEDATA_SNAPSHOTS", str(snaps))
    doc = decisions.snapshot("s1", "t", [], chains=chains)
    (snaps / "snapshots").mkdir(parents=True)
    (snaps / decisions.key("s1")).write_text(json.dumps(doc, sort_keys=True))
    seen = {}

    class Stop(Exception):
        pass

    def fake_build(_lake, _db, extra=None):
        seen.update(extra or {})
        raise Stop

    monkeypatch.setattr(cli, "dbt_build", fake_build)
    args = argparse.Namespace(db=str(tmp_path / "coffee.duckdb"), run_date="2026-10-19", accept_volume_change=False,
                              draft="d1", snapshot="s1", snapshot_sha256=decisions.digest(doc))  # fmt: skip
    with pytest.raises(Stop):
        cli._build(args, lake, tmp_path / "coffee.duckdb", "2026-10-19", "b1")
    assert seen.get("chain_flags") == chains
    assert ("chain_flags" in seen) == (chains is not None)
