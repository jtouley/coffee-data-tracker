<!--
Copyright 2026 The Bitol Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Keeping a contract and dbt models in sync

When dbt builds the tables a contract describes, two files make the same promises: the ODCS contract and
dbt's `schema.yml`. They drift unless something checks them. The pattern that works is to treat the
contract as the definition, have dbt enforce it, and add a test that fails when they disagree, so any change
to a public table starts in the contract.

## Mapping

| ODCS (per schema object / property) | dbt (per model / column) |
| --- | --- |
| object `name` | model `name`, with `config: {contract: {enforced: true}}` |
| property `name`, `physicalType` (in order) | `columns[].name`, `columns[].data_type` (same order) |
| `required: true` | `constraints: [{type: not_null}]` and a `not_null` test |
| `unique: true` | a `unique` test |
| `primaryKey` (single column) | `unique` + `not_null` (and a `primary_key` constraint where the adapter enforces it) |
| `primaryKey` on several properties | a model-level `unique` test over the combined columns |
| `enum` values | an `accepted_values` test with the same values |
| `relationships: [{to: other.col}]` | a `relationships` test (`to: ref('other')`, `field: col`) |
| `quality` rule `type: custom`, `engine: dbt`, `implementation: tests/x.sql` | the singular test file `tests/x.sql` |
| thresholds in `customProperties` / SLA `value` | `vars` in `dbt_project.yml` that the tests read |

Write `physicalType` exactly as the warehouse names the type (dbt's `data_type`), since the enforced
contract compares them.

## The check

A test in the project (pytest shown; any runner works) loads both files and asserts the mapping, plus
schema validity. A minimal version:

```python
import json, yaml, jsonschema
from pathlib import Path

contract = yaml.safe_load(Path("contracts/orders.odcs.yaml").read_text())
models = {m["name"]: m for m in yaml.safe_load(Path("dbt/models/schema.yml").read_text())["models"]}
schema = json.loads(Path("contracts/schemas/odcs-json-schema-v3.2.0.json").read_text())

def test_contract_is_valid_odcs():
    v = jsonschema.validators.validator_for(schema)(schema)
    assert [e.message for e in v.iter_errors(contract)] == []

def test_dbt_enforces_the_contract():
    for obj in contract["schema"]:
        model = models[obj["name"]]
        assert model["config"]["contract"] == {"enforced": True}
        assert [(c["name"], c["data_type"]) for c in model["columns"]] == \
               [(p["name"], p["physicalType"]) for p in obj["properties"]]
        tests = {c["name"]: [t if isinstance(t, str) else next(iter(t)) for t in c.get("data_tests") or []]
                 for c in model["columns"]}
        for p in obj["properties"]:
            if p.get("required"):
                assert "not_null" in tests[p["name"]], p["name"]
            if p.get("unique"):
                assert "unique" in tests[p["name"]], p["name"]

def test_quality_rules_point_at_real_dbt_tests():
    for obj in contract["schema"]:
        for q in obj.get("quality") or []:
            if q.get("engine") == "dbt":
                assert (Path("dbt") / q["implementation"]).is_file(), q["implementation"]
```

Vendor the JSON schema into the repo (with its source commit noted) so the test runs offline and doesn't
change under you; `scripts/validate_contract.py` finds a vendored copy automatically.

## Versioning

Bump the contract's `version` with semver as consumers would feel it: a new optional column is minor, a
removed or retyped column or a tightened rule is major. If an ODPS data product lists the contract in an
output port, keep the port's `contractId` and `version` in step, and test that too.

## Going the other way

To draft a contract *from* an existing dbt project: take columns and `data_type` from `schema.yml` (or the
compiled catalog for types), `required` from not_null, `unique` from unique tests, `enum` from
accepted_values, relationships from relationships tests, and descriptions from dbt docs. Then add the
enforced contract config and the check above so the two stay aligned from then on.
