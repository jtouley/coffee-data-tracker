"""The internal contracts between services, generated as ODCS v3.2.0 from lake.SCHEMAS, versioned and tested.

    raw         ingest -> lake          snapshots, bodies, fetches, and parked progress files
    manifest    every job -> control    run_manifests (the roasters an ingest sets out to fetch) and job_manifests
    enrichment  enrichment job -> lake  enrichments (Claude's llm rows; older lakes also hold crawl-time rules rows)

Each is written to contracts/internal/<name>.odcs.yaml, from lake.SCHEMAS (columns and types) and contract_docs
(descriptions and quality rules). They are internal: never published, and nothing reads them at runtime; the
public contract is contracts/public-catalog.odcs.yaml. A column change in lake.py fails tests/test_raw_contracts.py
until the contract is regenerated with a bumped version:

    python -m coffeedata.raw_contracts --write <contract> <new version>

`make contracts` (python -m coffeedata.raw_contracts --all) regenerates every contract at its committed version,
after a change to contract_docs that no consumer reads differently.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import yaml

from . import contract_docs
from .lake import FETCH_STATUSES, GLOBS, SCHEMAS
from .models import CatalogItem

DIR = Path(__file__).resolve().parents[2] / "contracts" / "internal"
REPO = "https://github.com/jtouley/coffee-data-tracker/blob/main/"
CONTRACTS: dict[str, dict[str, Any]] = {
    "raw": {
        "tables": ["snapshots", "bodies", "fetches"],
        "producer": "connector ingest jobs (crawl, crawl-retry), via the weekly merge",
        "producer_code": ["src/coffeedata/ingest.py", "src/coffeedata/connectors.py"],
        "consumers": [
            "dbt (stg_snapshots, stg_product_inputs, stg_fetches, int_fetch_product_inputs)",
            "enrichment job",
            "registry",
        ],
        # The values a column may take, versioned with the columns (1.2.0 added unavailable).
        "values": {"fetches.status": list(FETCH_STATUSES)},
        "files": {
            "parked/run_date=<date>/<roaster_id>.json": {
                "description": "A store parked mid-catalog for crawl-retry: where to resume and what was read.",
                "properties": {
                    "page": ("integer", "The catalog page that stayed blocked."),
                    "items": ("array", "The catalog items read before the block (CatalogItem fields)."),
                },
                "items": sorted(CatalogItem.model_fields),
            }
        },
    },
    "manifest": {
        "tables": ["run_manifests", "job_manifests"],
        "producer": "every job (crawl, crawl-retry, merge, enrich)",
        "producer_code": ["src/coffeedata/control.py", "src/coffeedata/ingest.py"],
        "consumers": ["dbt (run_completeness)", "merge", "enrichment job", "egress-check"],
    },
    "enrichment": {
        "tables": ["enrichments"],
        "producer": "post-load enrichment job (enrich-week)",
        "producer_code": ["src/coffeedata/enrich.py"],
        "consumers": ["dbt (stg_enrichments, dim_coffee)"],
    },
}


def _types(t: pa.DataType) -> dict[str, Any]:
    """ODCS logicalType (with items or properties for nested types) and physicalType for an Arrow type."""
    if pa.types.is_list(t):
        return {"logicalType": "array", "physicalType": str(t), "items": _types(t.value_type)}
    if pa.types.is_struct(t):
        fields = [{"name": f.name, **_types(f.type)} for f in t]
        return {"logicalType": "object", "physicalType": str(t), "properties": fields}
    if pa.types.is_timestamp(t):
        logical: dict[str, Any] = {"logicalType": "timestamp"}
        if t.tz:
            logical["logicalTypeOptions"] = {"timezone": True, "defaultTimezone": t.tz}
        return logical | {"physicalType": str(t)}
    for check, name in ((pa.types.is_date, "date"), (pa.types.is_integer, "integer"),
                        (pa.types.is_floating, "number"), (pa.types.is_boolean, "boolean"),
                        (pa.types.is_string, "string")):  # fmt: skip
        if check(t):
            return {"logicalType": name, "physicalType": str(t)}
    raise ValueError(f"no ODCS logical type for {t}")


def _quality(rule: dict) -> dict:
    out: dict[str, Any] = {"id": rule["id"], "description": rule["rule"] + "."}
    if rule.get("engine"):
        out |= {"type": "custom", "engine": rule["engine"], "implementation": rule["implementation"]}
    else:
        out["type"] = "text"
    return out | {
        "dimension": rule["dimension"],
        "severity": rule["severity"],
        "customProperties": [{"property": "enforcedBy", "value": rule["enforced_by"]}],
    }


def _table(name: str, table: str, values: dict[str, list[str]]) -> dict:
    required = contract_docs.REQUIRED.get(table, [])
    props = []
    for f in SCHEMAS[table]:
        p: dict[str, Any] = {"name": f.name, **_types(f.type)}
        p["required"] = not f.nullable or f.name in required
        p["description"] = contract_docs.COLUMNS[table][f.name]
        if f"{table}.{f.name}" in values:
            p["enum"] = [{"value": v} for v in values[f"{table}.{f.name}"]]
        props.append(p)
    quality = [_quality(q) for q in contract_docs.QUALITY[name] if table in q["tables"]]
    return {
        "name": table,
        "physicalName": table,
        "physicalType": "table",
        "description": contract_docs.TABLES[table],
        "properties": props,
        **({"quality": quality} if quality else {}),
        "customProperties": [{"property": "lakeGlob", "value": GLOBS[table]}],
    }


def _file(path: str, spec: dict) -> dict:
    props: list[dict[str, Any]] = []
    for prop, (logical, description) in spec["properties"].items():
        p: dict[str, Any] = {
            "name": prop,
            "logicalType": logical,
            "required": True,
            "description": description,
        }
        if prop == "items":
            p["items"] = {"logicalType": "object", "properties": [{"name": n} for n in spec["items"]]}
        props.append(p)
    return {"name": "parked", "physicalName": path, "physicalType": "file", "description": spec["description"],
            "properties": props}  # fmt: skip


def generate(name: str, version: str) -> dict:
    spec = CONTRACTS[name]
    values = spec.get("values", {})
    schema = [_table(name, t, values) for t in spec["tables"]]
    schema += [_file(p, f) for p, f in spec.get("files", {}).items()]
    links = [
        (
            "src/coffeedata/lake.py",
            "implementation",
            "lake.SCHEMAS: the Arrow schemas the columns and types come from",
        ),
        (
            "src/coffeedata/contract_docs.py",
            "businessDefinition",
            "Table and column descriptions, and quality rules",
        ),
        ("src/coffeedata/raw_contracts.py", "implementation", "The generator of this file"),
        *((p, "implementation", "Producer") for p in spec["producer_code"]),
        (
            "tests/test_raw_contracts.py",
            "implementation",
            "Tests that hold producers and this contract together",
        ),
    ]
    body: dict[str, Any] = {
        "name": f"Coffee Data internal {name} contract",
        "status": "active",
        "domain": "coffee",
        "tags": ["internal"],
        "description": {
            "purpose": f"Internal contract between services: {spec['producer']} -> {', '.join(spec['consumers'])}.",
            "usage": "Internal to the pipeline; not published, and not a public interface. Readers outside the "
            "pipeline use the public catalog contract (contracts/public-catalog.odcs.yaml).",
            "limitations": "Every lake column is nullable in the Arrow schema so that older files read back; "
            "required marks the columns a quality rule holds non-null.",
        },
        "schema": schema,
        "authoritativeDefinitions": [{"url": REPO + p, "type": t, "description": d} for p, t, d in links],
        "customProperties": [
            {"property": "audience", "value": "internal"},
            {"property": "producer", "value": spec["producer"]},
            {"property": "consumers", "value": spec["consumers"]},
        ],
    }
    # Over everything but the version, so the same content at a new version keeps its fingerprint.
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    body["customProperties"].append({"property": "fingerprint", "value": digest})
    head = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": f"coffee-data-internal-{name}",
        "version": version,
    }
    return head | body


HEADER = (
    "# Internal ODCS contract, generated by src/coffeedata/raw_contracts.py from lake.SCHEMAS and contract_docs.py.\n"
    "# Do not edit: change those and run `make contracts` (or python -m coffeedata.raw_contracts --write <name>\n"
    "# <version> for a new version). tests/test_raw_contracts.py fails when this file and the code disagree.\n"
)


def render(doc: dict) -> str:
    return HEADER + yaml.safe_dump(doc, sort_keys=False, width=118, allow_unicode=True)


def path(name: str) -> Path:
    return DIR / f"{name}.odcs.yaml"


def load(name: str) -> dict:
    return yaml.safe_load(path(name).read_text())


def fingerprint(doc: dict) -> str:
    return next(p["value"] for p in doc["customProperties"] if p["property"] == "fingerprint")


def columns(name: str, table: str) -> list[str]:
    """The contracted columns of one table, in order."""
    return [p["name"] for p in next(o for o in load(name)["schema"] if o["name"] == table)["properties"]]


def write(name: str, version: str) -> Path:
    DIR.mkdir(parents=True, exist_ok=True)
    path(name).write_text(render(generate(name, version)))
    return path(name)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--write"]:
        print(write(sys.argv[2], sys.argv[3]))
    elif sys.argv[1:2] == ["--all"]:  # every contract at its committed version
        for n in CONTRACTS:
            print(write(n, load(n)["version"]))
    else:
        sys.exit("usage: python -m coffeedata.raw_contracts --write <contract> <version> | --all")
