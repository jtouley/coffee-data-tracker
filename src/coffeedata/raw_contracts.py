"""The internal contracts between services, generated as ODCS v3.2.0 from lake.SCHEMAS, versioned and tested.

    raw         ingest -> lake          snapshots, bodies, fetches, and parked progress files
    manifest    every job -> control    run_manifests (the roasters an ingest sets out to fetch) and job_manifests
    enrichment  enrichment job -> lake  enrichments (Claude's llm rows; older lakes also hold crawl-time rules rows)
    ops         build -> command center run_completeness, release_changes and product_decisions, beside each release
    usage       collector -> usage lake usage_events (anonymous search, filter and click-out events; private)

Each is written to contracts/internal/<name>.odcs.yaml, from lake.SCHEMAS (columns and types) and contract_docs
(descriptions and quality rules). They are internal: never published, and nothing reads them at runtime; the
public contract is contracts/public-catalog.odcs.yaml.

Each carries two fingerprints. The interface fingerprint is over what consumers see: object and column names,
types, required and enums. The docs fingerprint is over everything else (descriptions, quality rules, links).
contracts/internal/interfaces.yaml records the interface fingerprint of every released version, so a version
names one interface for good:

    make contracts                                              # python -m coffeedata.raw_contracts --all
        regenerates every contract at its committed version; refuses if an interface changed (a doc-only change
        is fine)
    python -m coffeedata.raw_contracts --write <contract> <new version>
        after an interface change: writes the new version and records its interface fingerprint
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
from .lake import FETCH_STATUSES, GLOBS, OPS_GLOBS, SCHEMAS, USAGE_VALUES
from .models import CatalogItem

# The repo's contracts/internal/: this assumes a source or editable install, which is fine because only the
# generator and the tests import this module (nothing reads the contracts at runtime).
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
        # The values a column may take, versioned with the columns (1.2.0 added unavailable, 1.3.0 crashed).
        "values": {"fetches.status": list(FETCH_STATUSES)},
        "files": {
            "parked/run_date=<date>/<roaster_id>.json": {
                "description": "A store parked mid-catalog for crawl-retry: where to resume and what was read.",
                "properties": {
                    "page": ("integer", "The catalog page that stayed blocked."),
                    "items": ("array", "The catalog items read before the block (CatalogItem fields)."),
                },
                "items": sorted(CatalogItem.model_fields),
            },
            "raw/connector=<name>/run_date=<week>/_inflight/attempt=<attempt_id>/<roaster_id>.json": {
                "name": "inflight",
                "description": "A roaster in flight in a crawl attempt (crashguard): written before its fetch, "
                "removed when it returns, and the attempt's folder removed when it ends normally. The folder of an "
                "attempt without a done or deadline end row holds the roasters in flight when it died; the next "
                "attempt of the week quarantines one found in two such folders (fetches.status crashed).",
                "properties": {
                    "roaster_id": ("string", "The roaster (also the file name)."),
                    "started_at": ("string", "When its fetch started (ISO 8601, UTC)."),
                },
            },
        },
    },
    "manifest": {
        "tables": ["run_manifests", "job_manifests"],
        "producer": "every job (discover, probe, crawl, crawl-retry, merge, enrich, fetch-health, classify, build)",
        "producer_code": [
            "src/coffeedata/control.py",
            "src/coffeedata/cli.py",
            "src/coffeedata/ingest.py",
            "src/coffeedata/fetchhealth.py",
        ],
        "consumers": [
            "dbt (run_completeness)",
            "merge",
            "enrichment job",
            "egress-check",
            "fetch-health (when a pass ended), and the crawl passes after it (its list, only with re-fetch on: "
            "COFFEEDATA_DQ_REFETCH, off by default)",
        ],
    },
    "ops": {
        "tables": ["run_completeness", "release_changes", "product_decisions"],
        "producer": "coffeedata build (quality.write_ops: the build's dbt models, beside its release under ops/)",
        "producer_code": [
            "src/coffeedata/quality.py",
            "dbt/models/marts/run_completeness.sql",
            "dbt/models/marts/release_changes.sql",
            "dbt/models/marts/product_decisions.sql",
        ],
        "consumers": ["the owner's command center (admin.registry.coffee)"],
    },
    "enrichment": {
        "tables": ["enrichments"],
        "producer": "post-load enrichment job (enrich-week)",
        "producer_code": ["src/coffeedata/enrich.py"],
        "consumers": ["dbt (stg_enrichments, dim_coffee)"],
    },
    "usage": {
        "tables": ["usage_events"],
        "producer": "the usage collector (app/usage.py, POST /e on its run.app address)",
        "producer_code": ["app/usage.py", "app/usage_events.py"],
        # What the page scripts send the collector: the envelope, each event's fields and the collector's rules.
        "links": [
            (
                "contracts/internal/usage-wire.schema.json",
                "The wire format the page scripts send (JSON Schema)",
            )
        ],
        "consumers": ["dbt (models/usage/: stg_usage_events, int_usage_location)"],
        "values": {f"usage_events.{c}": list(v) for c, v in USAGE_VALUES.items()},
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


def _quality(rule: dict, table: str) -> dict:
    # A rule on several tables is emitted on each, so its id names the table to stay unique in the contract.
    rule_id = f"{table}_{rule['id']}" if len(rule["tables"]) > 1 else rule["id"]
    out: dict[str, Any] = {"id": rule_id, "description": rule["rule"] + "."}
    if rule.get("engine"):
        out |= {"type": "custom", "engine": rule["engine"], "implementation": rule["implementation"]}
    else:
        out["type"] = "text"
    # A pytest rule's enforcer is its implementation; dbt and text rules name theirs.
    enforced_by = rule.get("enforced_by") or rule["implementation"]
    return out | {
        "dimension": rule["dimension"],
        "severity": rule["severity"],
        "customProperties": [{"property": "enforcedBy", "value": enforced_by}],
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
    quality = [_quality(q, table) for q in contract_docs.QUALITY[name] if table in q["tables"]]
    return {
        "name": table,
        "physicalName": table,
        "physicalType": "table",
        "description": contract_docs.TABLES[table],
        "properties": props,
        **({"quality": quality} if quality else {}),
        "customProperties": [{"property": "lakeGlob", "value": (GLOBS | OPS_GLOBS)[table]}],
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
        if prop == "items" and "items" in spec:
            p["items"] = {"logicalType": "object", "properties": [{"name": n} for n in spec["items"]]}
        props.append(p)
    return {"name": spec.get("name", "parked"), "physicalName": path, "physicalType": "file", "description": spec["description"],
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
        *((p, "implementation", d) for p, d in spec.get("links", [])),
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
    # Neither covers the version, so the same content at a new version keeps its fingerprints.
    body["customProperties"] += [
        {"property": "interfaceFingerprint", "value": _digest(_interface(schema))},
        {"property": "docsFingerprint", "value": _digest(_strip(body))},
    ]
    head = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": f"coffee-data-internal-{name}",
        "version": version,
    }
    return head | body


# What a consumer reads: names, types, required and enums. Everything else is documentation.
_INTERFACE = ("logicalType", "logicalTypeOptions", "physicalType", "required", "enum")


def _interface(node: Any) -> Any:
    """The consumer-visible part of a schema: each object's and property's name, types, required and enum."""
    if isinstance(node, list):
        return [_interface(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: node[k] for k in ("name", "physicalName", *_INTERFACE) if k in node}
    return out | {k: _interface(node[k]) for k in ("items", "properties") if k in node}


def _strip(node: Any) -> Any:
    """Everything but the interface: the document with types, required and enums taken out (names kept)."""
    if isinstance(node, list):
        return [_strip(n) for n in node]
    if not isinstance(node, dict):
        return node
    return {k: _strip(v) for k, v in node.items() if k not in _INTERFACE}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


HEADER = (
    "# Internal ODCS contract, generated by src/coffeedata/raw_contracts.py from lake.SCHEMAS and contract_docs.py.\n"
    "# Do not edit: change those and run `make contracts`, or python -m coffeedata.raw_contracts --write <name>\n"
    "# <new version> after a change to names, types, required or enums. tests/test_raw_contracts.py fails when this\n"
    "# file and the code disagree.\n"
)


def render(doc: dict) -> str:
    return HEADER + yaml.safe_dump(doc, sort_keys=False, width=118, allow_unicode=True)


def path(name: str) -> Path:
    return DIR / f"{name}.odcs.yaml"


def load(name: str) -> dict:
    return yaml.safe_load(path(name).read_text())


def fingerprint(doc: dict, kind: str = "interface") -> str:
    """A contract's interface or docs fingerprint."""
    return next(p["value"] for p in doc["customProperties"] if p["property"] == f"{kind}Fingerprint")


def interfaces_path() -> Path:
    return DIR / "interfaces.yaml"


def interfaces() -> dict[str, dict[str, str]]:
    """Every released version of each contract and its interface fingerprint."""
    return yaml.safe_load(interfaces_path().read_text()) if interfaces_path().is_file() else {}


def _semver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


class VersionError(Exception):
    """A contract's interface changed without a new version."""


def columns(name: str, table: str) -> list[str]:
    """The contracted columns of one table, in order."""
    return [p["name"] for p in next(o for o in load(name)["schema"] if o["name"] == table)["properties"]]


def check(name: str, version: str, released: dict[str, dict[str, str]]) -> dict:
    """The contract at this version, or VersionError if that would give a released version a new interface or
    release a version that isn't above the last one."""
    doc = generate(name, version)
    known = released.get(name, {})
    if version in known:
        if known[version] != fingerprint(doc):
            raise VersionError(
                f"{name} {version}: the interface (names, types, required or enums) differs from the one released "
                f"as {version}. Write a new version: python -m coffeedata.raw_contracts --write {name} <new version>"
            )
    elif known and _semver(version) <= max(map(_semver, known)):
        raise VersionError(
            f"{name} {version}: not above the last released version, {max(known, key=_semver)}"
        )
    return doc


def write(name: str, version: str) -> Path:
    return write_all({name: version})[0]


def write_all(versions: dict[str, str]) -> list[Path]:
    """Write each contract at its version, after checking them all, and record the released interfaces."""
    released = interfaces()
    docs = {n: check(n, v, released) for n, v in versions.items()}
    DIR.mkdir(parents=True, exist_ok=True)
    for n, doc in docs.items():
        path(n).write_text(render(doc))
        released.setdefault(n, {})[doc["version"]] = fingerprint(doc)
    interfaces_path().write_text(
        "# Each internal contract's released versions and their interface fingerprints, written by\n"
        "# src/coffeedata/raw_contracts.py. A version's interface never changes; a new interface needs a new version.\n"
        + yaml.safe_dump(
            {n: dict(sorted(v.items(), key=lambda kv: _semver(kv[0]))) for n, v in released.items()},
            sort_keys=False,
        )
    )
    return [path(n) for n in docs]


def main(argv: list[str]) -> None:
    try:
        if argv[:1] == ["--write"] and len(argv) == 3:
            print(write(argv[1], argv[2]))
        elif argv == ["--all"]:  # every contract at its committed version
            print(*write_all({n: load(n)["version"] for n in CONTRACTS}), sep="\n")
        else:
            sys.exit("usage: python -m coffeedata.raw_contracts --write <contract> <version> | --all")
    except VersionError as e:
        sys.exit(f"refused: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
