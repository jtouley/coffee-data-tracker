"""The internal service contracts (coffeedata/raw_contracts.py, ODCS YAML in contracts/internal/): each committed
file is what the generator writes from lake.SCHEMAS, is valid ODCS v3.2.0, and each producer writes exactly its
contract's columns. Changing a column, type, required or enum fails here until the contract is written at a new
version; `make contracts` refuses to write it at the old one."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

from coffeedata import contract_docs, control, enrich, ingest, raw_contracts
from coffeedata.http import RequestStats
from coffeedata.lake import SCHEMAS, Lake
from coffeedata.models import CatalogItem, Enrichment, Roaster

from .test_contracts import _validator


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_the_committed_contract_matches_the_lake_schemas(name):
    committed = raw_contracts.load(name)
    now = raw_contracts.generate(name, committed["version"])
    assert raw_contracts.fingerprint(now) == raw_contracts.fingerprint(committed), (
        f"{name} interface changed: run python -m coffeedata.raw_contracts --write {name} <new version>"
    )
    assert now == committed, f"{name} docs changed: run make contracts"


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_regenerating_the_contract_changes_nothing(name):
    """The drift guard: the committed file is byte for byte what `make contracts` writes."""
    text = raw_contracts.path(name).read_text()
    assert raw_contracts.render(raw_contracts.generate(name, yaml.safe_load(text)["version"])) == text, (
        f"contracts/internal/{name}.odcs.yaml is stale: run make contracts"
    )


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_each_version_keeps_the_interface_it_was_released_with(name):
    """The missing-bump guard: interfaces.yaml records each released version's interface fingerprint, and the code's
    interface must be the one recorded for the committed version. It is the latest released version."""
    released = raw_contracts.interfaces()[name]
    version = raw_contracts.load(name)["version"]
    assert version == max(released, key=lambda v: tuple(map(int, v.split("."))))
    assert raw_contracts.fingerprint(raw_contracts.generate(name, version)) == released[version], (
        f"{name} {version} was released with another interface: "
        f"run python -m coffeedata.raw_contracts --write {name} <new version>"
    )


@pytest.fixture
def scratch_contracts(tmp_path, monkeypatch):
    """A copy of contracts/internal/ for the generator to write into."""
    for f in raw_contracts.DIR.iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    monkeypatch.setattr(raw_contracts, "DIR", tmp_path)
    return tmp_path


def _add_a_bodies_column(monkeypatch):
    monkeypatch.setitem(SCHEMAS, "bodies", SCHEMAS["bodies"].append(pa.field("language", pa.string())))
    monkeypatch.setitem(
        contract_docs.COLUMNS, "bodies", contract_docs.COLUMNS["bodies"] | {"language": "Its language."}
    )


def test_make_contracts_refuses_a_new_column_without_a_version_bump(scratch_contracts, monkeypatch):
    """The review's case: add a column, describe it, run make contracts at the same version: refused, nothing written."""
    before = {f.name: f.read_text() for f in scratch_contracts.iterdir()}
    _add_a_bodies_column(monkeypatch)
    with pytest.raises(SystemExit, match=r"refused: raw 1\.4\.0: the interface .* --write raw <new version>"):
        raw_contracts.main(["--all"])
    with pytest.raises(SystemExit, match="refused: raw 1.4.0"):
        raw_contracts.main(["--write", "raw", "1.4.0"])
    with pytest.raises(SystemExit, match="not above the last released version"):
        raw_contracts.main(["--write", "raw", "1.3.9"])
    assert {f.name: f.read_text() for f in scratch_contracts.iterdir()} == before


def test_a_new_column_ships_at_a_new_version(scratch_contracts, monkeypatch):
    _add_a_bodies_column(monkeypatch)
    raw_contracts.main(["--write", "raw", "1.5.0"])
    doc = raw_contracts.load("raw")
    assert doc["version"] == "1.5.0" and "language" in raw_contracts.columns("raw", "bodies")
    released = raw_contracts.interfaces()["raw"]
    assert list(released) == ["1.2.0", "1.3.0", "1.4.0", "1.5.0"] and released[
        "1.5.0"
    ] == raw_contracts.fingerprint(doc)
    assert released["1.4.0"] != released["1.5.0"]
    raw_contracts.main(["--all"])  # and regenerates at 1.5.0 from then on
    assert raw_contracts.load("raw")["version"] == "1.5.0"


def test_make_contracts_regenerates_a_doc_only_change_at_the_same_version(scratch_contracts, monkeypatch):
    committed = raw_contracts.load("raw")
    monkeypatch.setitem(contract_docs.TABLES, "bodies", "Each product description once, by hash.")
    monkeypatch.setitem(
        contract_docs.COLUMNS, "bodies", contract_docs.COLUMNS["bodies"] | {"body_text": "The text."}
    )
    raw_contracts.main(["--all"])
    doc = raw_contracts.load("raw")
    assert doc["version"] == committed["version"]
    assert raw_contracts.fingerprint(doc) == raw_contracts.fingerprint(committed)
    assert raw_contracts.fingerprint(doc, "docs") != raw_contracts.fingerprint(committed, "docs")
    bodies = next(o for o in doc["schema"] if o["name"] == "bodies")
    assert bodies["description"] == "Each product description once, by hash."
    assert raw_contracts.interfaces()["raw"][committed["version"]] == raw_contracts.fingerprint(committed)


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_quality_rule_ids_are_unique_in_each_contract(name):
    ids = [q["id"] for o in raw_contracts.load(name)["schema"] for q in o.get("quality") or []]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_required_columns_are_the_ones_a_not_null_rule_holds():
    for table, required in contract_docs.REQUIRED.items():
        held = [c for q in contract_docs.QUALITY["raw"] if table in q["tables"] and q.get("engine") == "dbt"
                and isinstance(q["implementation"], dict) and q["implementation"]["test"] == "not_null"
                for c in q["implementation"]["columns"]]  # fmt: skip
        assert sorted(held) == sorted(required), table


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_each_contract_is_valid_internal_odcs(name):

    doc = raw_contracts.load(name)
    v = _validator("odcs-json-schema-v3.2.0.json")
    assert [e.message for e in v.iter_errors(doc)] == []
    assert list(v.iter_errors(doc | {"kind": "NotAContract"}))  # the validator does catch a bad document
    assert doc["id"] == f"coffee-data-internal-{name}" and "internal" in doc["tags"]
    assert {"property": "audience", "value": "internal"} in doc["customProperties"]
    links = [d["url"] for d in doc["authoritativeDefinitions"]]
    root = Path(__file__).parent.parent
    assert links and all((root / u.removeprefix(raw_contracts.REPO)).is_file() for u in links), links


def _cols(name: str, table: str) -> list[str]:
    return raw_contracts.columns(name, table)


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
    doc = json.loads(ingest.parked_path(lake, "2026-11-02", "x.com").read_text())
    parked = next(o for o in raw_contracts.load("raw")["schema"] if o["name"] == "parked")
    assert parked["physicalName"] == "parked/run_date=<date>/<roaster_id>.json"
    shape = {p["name"]: p for p in parked["properties"]}
    assert set(doc) == set(shape)
    assert set(doc["items"][0]) == {p["name"] for p in shape["items"]["items"]["properties"]}
    assert ingest.load_parked(lake, "2026-11-02")["x.com"]["page"] == 2


def test_inflight_markers_follow_the_raw_contract(tmp_path):
    from coffeedata import connectors, crashguard

    part = connectors.partition(Lake(str(tmp_path / "lake")), "generic", "2026-11-02")
    guard = crashguard.Guard(crashguard.root(part), "a1")
    guard.start("x.com")
    spec = next(o for o in raw_contracts.load("raw")["schema"] if o["name"] == "inflight")
    rel = (guard.dir / "x.com.json").relative_to(part.parents[2]).as_posix()
    assert rel == "raw/connector=generic/run_date=2026-11-02/_inflight/attempt=a1/x.com.json"
    assert (
        spec["physicalName"]
        == "raw/connector=<name>/run_date=<week>/_inflight/attempt=<attempt_id>/<roaster_id>.json"
    )
    assert set(json.loads((guard.dir / "x.com.json").read_text())) == {p["name"] for p in spec["properties"]}


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


def test_fetch_statuses_are_the_raw_contracts():
    """ok, error, unavailable (raw 1.2.0) and crashed (1.3.0): every status a crawl writes is in the contract."""
    fetches = next(o for o in raw_contracts.load("raw")["schema"] if o["name"] == "fetches")
    status = next(p for p in fetches["properties"] if p["name"] == "status")
    assert [e["value"] for e in status["enum"]] == ["ok", "error", "unavailable", "crashed"]
    r = Roaster(roaster_id="x.com", name="X", host="x.com", platform="shopify")
    now = datetime(2026, 11, 2, tzinfo=UTC)
    results = [ingest.Result(r, now, stats=RequestStats()), ingest.Result(r, now, error="boom", stats=RequestStats()),
               ingest.Result(r, now, error="Unavailable: closed", unavailable=True, stats=RequestStats()),
               ingest.Result(r, now, error="crashed: in flight when 2 attempts died", quarantined=True)]  # fmt: skip
    rows = ingest.rows_for(results, date(2026, 11, 2), "b", set())["fetches"]
    assert [x["status"] for x in rows] == ["ok", "error", "unavailable", "crashed"]
    # an ok fetch says whether it read the whole catalog (False: complete); the others have no catalog to qualify
    assert [x["partial"] for x in rows] == [False, None, None, None]


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_every_contracted_table_and_column_is_described_and_has_quality_rules(name):
    doc = raw_contracts.load(name)
    tables = {o["name"]: o for o in doc["schema"] if o["physicalType"] == "table"}
    assert list(tables) == raw_contracts.CONTRACTS[name]["tables"]
    for table, obj in tables.items():
        assert obj["description"]
        props = {p["name"]: p for p in obj["properties"]}
        assert list(props) == SCHEMAS[table].names, f"{table}: contract != lake.SCHEMAS"
        assert all(p["description"] and p["logicalType"] and p["physicalType"] for p in props.values()), table
        assert set(contract_docs.COLUMNS[table]) == set(SCHEMAS[table].names), f"{table}: described != lake"
        assert set(contract_docs.REQUIRED.get(table, [])) <= set(props), table
    rules = [q for o in doc["schema"] for q in o.get("quality") or []]
    assert rules and all(q["description"] and q["dimension"] and q["severity"] for q in rules)
    assert all(
        any(c["property"] == "enforcedBy" and c["value"] for c in q["customProperties"]) for q in rules
    )


_ROOT = Path(__file__).parent.parent
_DBT_MODELS = {m["name"]: m for m in yaml.safe_load((_ROOT / "dbt/models/schema.yml").read_text())["models"]}


def _dbt_tests(model: str, column: str) -> list[str]:
    col = next(c for c in _DBT_MODELS[model]["columns"] if c["name"] == column)
    return [t if isinstance(t, str) else next(iter(t)) for t in col.get("data_tests") or []]


@pytest.mark.parametrize("name", sorted(raw_contracts.CONTRACTS))
def test_each_quality_rule_names_an_enforcer_that_exists(name):
    for q in contract_docs.QUALITY[name]:
        impl = q.get("implementation")
        if q.get("engine") == "dbt" and isinstance(impl, str):
            assert (_ROOT / "dbt" / impl).is_file(), impl
        elif q.get("engine") == "dbt":
            assert (_ROOT / "dbt" / impl["file"]).is_file()
            assert all(impl["test"] in _dbt_tests(impl["model"], c) for c in impl["columns"]), impl
        elif q.get("engine") == "pytest":
            file, test = impl.split("::")
            assert re.search(rf"^def {test}\(", (_ROOT / file).read_text(), re.M), impl
        else:
            assert "engine" not in q and q["enforced_by"], q
