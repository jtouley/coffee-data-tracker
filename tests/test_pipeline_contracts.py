"""The ingestion contracts Design C's services share, pinned: raw (connector jobs -> merge), manifest/control (every
job -> sweep, HTML fallback, merge), enrichment (post-load enrichment -> dbt). The public contract is
contracts/public-catalog.odcs.yaml, checked by tests/test_contracts.py. Changing a pinned shape means a new version."""

import json

import pyarrow as pa
import pyarrow.parquet as pq

from coffeedata import ingest, pipelines
from coffeedata.lake import SCHEMAS, Lake

from .test_pipelines import _job


def test_raw_contract_v1(tmp_path):
    assert pipelines.RAW_CONTRACT == "raw/v1"
    assert [(f.name, str(f.type)) for f in pipelines.RAW] == [
        ("kind", "string"),
        ("roaster_id", "string"),
        ("row", "string"),
    ]
    lake = Lake(url=str(tmp_path / "lake"))
    _job(lake, tmp_path / "bucket", "2026-11-03")
    (p,) = (tmp_path / "bucket/raw").rglob("data.parquet")
    assert pq.read_schema(p).metadata[b"contract"] == b"raw/v1"
    rows = pq.read_table(p).to_pylist()
    assert {r["kind"] for r in rows} <= set(pipelines.RAW_KINDS)  # ELT: no enrichments from a fetch job
    for r in rows:  # every row is a row of its lake table, and reads back as one
        row = json.loads(r["row"])
        assert set(row) <= set(SCHEMAS[r["kind"]].names), r["kind"]
        pa.Table.from_pylist([pipelines._revive(r["kind"], row)], schema=SCHEMAS[r["kind"]])


def test_control_contract_v1(tmp_path):
    assert pipelines.CONTROL_CONTRACT == "control/v1"
    assert pipelines.CONTROL.names == [
        "job_id", "job", "connector", "shard", "run_date", "ended_at", "roaster_id", "outcome", "error",
        "fetched_at", "retry_at", "requests", "rate_limited",
    ]  # fmt: skip
    lake = Lake(url=str(tmp_path / "lake"))
    out, outcomes = _job(lake, tmp_path / "bucket", "2026-11-03")
    pipelines.write_control(tmp_path / "bucket", "2026-11-03", "j", outcomes.rows())
    (p,) = (tmp_path / "bucket/control").rglob("*.parquet")
    assert pq.read_schema(p).metadata[b"contract"] == b"control/v1"
    assert {r["outcome"] for r in pq.read_table(p).to_pylist()} <= set(pipelines.OUTCOMES)


def test_enrichment_contract(tmp_path, fixture_items):
    """The post-load job writes the enrichments table's exact columns, the same rows ingest would have."""
    assert SCHEMAS["enrichments"].names == [
        "extracted_at", "roaster_id", "product_id", "body_hash", "method", "needs_llm", "origin_country",
        "origin_region", "producer", "process", "varietals", "elevation_masl_min", "elevation_masl_max",
        "roast_level", "tasting_notes", "is_blend", "is_decaf",
    ]  # fmt: skip
    item = next(iter(fixture_items.values()))[0]
    row = ingest.rules_row(item, item.roaster_id, None)
    assert set(row) == set(SCHEMAS["enrichments"].names) and row["method"] == "rules"
