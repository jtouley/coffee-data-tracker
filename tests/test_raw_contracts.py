"""The internal service contracts (coffeedata/raw_contracts.py): each committed file matches lake.SCHEMAS, and each
producer writes exactly its contract's columns. Changing a raw column fails here until the contract is
regenerated with a new version."""

from datetime import UTC, date, datetime

import pytest

from coffeedata import control, enrich, ingest, raw_contracts
from coffeedata.http import RequestStats
from coffeedata.lake import Lake
from coffeedata.models import CatalogItem, Enrichment, Roaster


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_the_committed_contract_matches_the_lake_schemas(name):
    committed = raw_contracts.load(name)
    now = raw_contracts.generate(name, committed["version"])
    assert now["fingerprint"] == committed["fingerprint"], (
        f"{name} contract changed: run python -m coffeedata.raw_contracts --write {name} <new version>"
    )
    assert now == committed


def _cols(name: str, table: str) -> list[str]:
    return [c["name"] for c in raw_contracts.load(name)["tables"][table]]


def test_ingest_writes_exactly_the_raw_contract():
    r = Roaster(roaster_id="x.com", name="X", host="x.com", platform="shopify")
    item = CatalogItem(roaster_id="x.com", product_id="1", handle="h", url="u", title="Coffee", platform="shopify",
                       body_hash="b1", body_text="text")  # fmt: skip
    res = ingest.Result(r, datetime(2026, 11, 2, tzinfo=UTC), items=[item], stats=RequestStats())
    rows = ingest.rows_for([res], date(2026, 11, 2), "b", set())
    assert set(rows) == set(raw_contracts.CONTRACTS["raw"]["tables"])  # no enrichment rows from a crawl (ELT)
    for table, out in rows.items():
        assert out and all(set(row) <= set(_cols("raw", table)) for row in out), table
    assert set(rows["fetches"][0]) == set(_cols("raw", "fetches"))
    assert set(rows["bodies"][0]) == set(_cols("raw", "bodies"))


def test_parked_files_follow_the_raw_contract(tmp_path):
    lake = Lake(str(tmp_path / "lake"))
    item = CatalogItem(roaster_id="x.com", product_id="1", handle="h", url="u", title="C", platform="shopify")
    ingest.save_parked(lake, "2026-11-02", "x.com", {"page": 2, "items": [item]})
    import json

    doc = json.loads(ingest.parked_path(lake, "2026-11-02", "x.com").read_text())
    shape = raw_contracts.load("raw")["files"]["parked/run_date=<date>/<roaster_id>.json"]
    assert set(doc) == set(shape) and set(doc["items"][0]) == set(shape["items"])
    assert ingest.load_parked(lake, "2026-11-02")["x.com"]["page"] == 2


def test_control_rows_follow_the_manifest_contract(tmp_path):
    lake = Lake(str(tmp_path / "lake"))
    control.finish(lake, control.start(lake, "crawl", "2026-11-02", "shopify", 3), "done", ok=2, errors=1,
                   first_contact={"stores": 3, "refused": 1})  # fmt: skip
    row = control.rows(lake, "2026-11-02")[0]
    assert list(row) == _cols("manifest", "job_manifests")
    assert row["refusals"] == [{"ip": "unknown", "stores": 3, "refused": 1}]
    assert set(
        ingest.manifest_rows([Roaster(roaster_id="x.com", name="X", host="x.com")], date(2026, 11, 2), "m")[0]
    ) == set(_cols("manifest", "run_manifests"))


def test_enrichment_rows_follow_the_enrichment_contract():
    row = enrich._row({"roaster_id": "x.com", "product_id": "1", "body_hash": "b"}, Enrichment(method="llm"),
                      datetime(2026, 11, 2, tzinfo=UTC))  # fmt: skip
    assert set(row) == set(_cols("enrichment", "enrichments"))
