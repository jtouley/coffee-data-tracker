"""The ODCS contract (contracts/public-catalog.odcs.yaml) is the definition of the public tables; dbt enforces it.

These tests fail when the two disagree, so a change to a public table starts in the contract: columns and types
(dbt's enforced contract), required and key columns (not_null and unique tests), allowed values
(accepted_values), the quality rules (each names the dbt test that enforces it) and the thresholds (dbt vars).
Both the contract and the ODPS product file must also be valid against Bitol's published JSON schemas.
"""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).parent.parent
CONTRACTS = ROOT / "contracts"
ODCS = yaml.safe_load((CONTRACTS / "public-catalog.odcs.yaml").read_text())
ODPS = yaml.safe_load((CONTRACTS / "coffee-data.odps.yaml").read_text())
DBT_MODELS = {m["name"]: m for m in yaml.safe_load((ROOT / "dbt/models/schema.yml").read_text())["models"]}
DBT_VARS = yaml.safe_load((ROOT / "dbt/dbt_project.yml").read_text())["vars"]


def _validator(name: str):
    schema = json.loads((CONTRACTS / "schemas" / name).read_text())
    return jsonschema.validators.validator_for(schema)(schema)


@pytest.mark.parametrize(
    "doc, schema",
    [(ODCS, "odcs-json-schema-v3.2.0.json"), (ODPS, "odps-json-schema-v1.1.0.json")],
    ids=["odcs", "odps"],
)
def test_documents_follow_the_bitol_standards(doc, schema):
    v = _validator(schema)
    assert [e.message for e in v.iter_errors(doc)] == []
    assert list(v.iter_errors(doc | {"apiVersion": "v9.9.9"}))  # the validator does catch a bad document


def test_the_product_serves_this_contract():
    assert [p["contractId"] for p in ODPS["outputPorts"]] == [ODCS["id"]]
    assert ODCS["dataProduct"] == ODPS["id"]


def _tests(cols: list) -> dict[str, list]:
    """dbt column tests by column name: plain names, and {test_name: config} for configured ones."""
    out: dict[str, list] = {}
    for c in cols:
        out[c["name"]] = [t if isinstance(t, str) else next(iter(t)) for t in c.get("data_tests") or []]
    return out


@pytest.mark.parametrize("obj", ODCS["schema"], ids=lambda o: o["name"])
def test_dbt_enforces_each_table_as_contracted(obj):
    model = DBT_MODELS[obj["name"]]
    assert (model.get("config") or {}).get("contract") == {"enforced": True}
    dbt_cols = [(c["name"], c["data_type"]) for c in model["columns"]]
    assert dbt_cols == [(p["name"], p["physicalType"]) for p in obj["properties"]], "columns or types differ"

    tests = _tests(model["columns"])
    keys = sorted((p for p in obj["properties"] if p.get("primaryKey")), key=lambda p: p["primaryKeyPosition"])
    for p in obj["properties"]:
        if p.get("required"):
            assert "not_null" in tests[p["name"]], f"{p['name']} is required by the contract"
        if p.get("unique"):
            assert "unique" in tests[p["name"]], f"{p['name']} is unique by the contract"
        if p.get("enum"):
            accepted = next(
                t["accepted_values"]
                for t in next(c for c in model["columns"] if c["name"] == p["name"])["data_tests"]
                if isinstance(t, dict) and "accepted_values" in t
            )
            assert sorted(accepted["arguments"]["values"]) == sorted(e["value"] for e in p["enum"])
    if len(keys) > 1:  # a composite key: one model-level unique test over all its columns
        grain = [
            t["unique"]["arguments"]["column_name"] for t in model.get("data_tests") or [] if "unique" in t
        ]
        assert any(all(k["name"] in g for k in keys) for g in grain), "composite key has no unique test"


def test_quality_rules_name_dbt_tests_that_exist():
    rules = [q for obj in ODCS["schema"] for q in obj.get("quality") or []]
    assert rules
    for q in rules:
        assert q["engine"] == "dbt" and (ROOT / "dbt" / q["implementation"]).is_file(), q["implementation"]


def test_thresholds_are_the_ones_dbt_uses():
    custom = {p["property"]: p["value"] for p in ODCS["customProperties"]}
    for var in ("volume_tolerance", "min_attempted_share", "min_ok_share", "max_run_age_days"):
        assert custom[var] == DBT_VARS[var], var
    sla = {p["property"]: p for p in ODCS["slaProperties"]}
    assert (sla["latency"]["value"], sla["latency"]["unit"]) == (DBT_VARS["max_run_age_days"], "d")
