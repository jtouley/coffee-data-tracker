"""The internal contracts between services, generated from lake.SCHEMAS, versioned and checked by tests.

    raw         ingest -> lake          snapshots, bodies, fetches, and parked progress files
    manifest    every job -> control    run_manifests (the roasters an ingest sets out to fetch) and job_manifests
    enrichment  enrichment job -> lake  enrichments (Claude's llm rows; older lakes also hold crawl-time rules rows)

The fourth, the public contract, is contracts/public-catalog.odcs.yaml (unchanged). A column change in lake.py
fails tests/test_raw_contracts.py until the contract file is regenerated with a bumped version:

    python -m coffeedata.raw_contracts --write <contract> <new version>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from . import contract_docs
from .lake import FETCH_STATUSES, SCHEMAS
from .models import CatalogItem

DIR = Path(__file__).parent / "contracts"
CONTRACTS: dict[str, dict[str, Any]] = {
    "raw": {
        "tables": ["snapshots", "bodies", "fetches"],
        "producer": "connector ingest jobs (crawl, crawl-retry), via the weekly merge",
        "consumers": ["dbt (stg_snapshots, stg_product_inputs, stg_fetches)", "enrichment job", "registry"],
        # The values a column may take, versioned with the columns (1.2.0 added unavailable).
        "values": {"fetches.status": list(FETCH_STATUSES)},
        "files": {
            "parked/run_date=<date>/<roaster_id>.json": {
                "page": "int: the catalog page that stayed blocked",
                "items": sorted(CatalogItem.model_fields),
            }
        },
    },
    "manifest": {
        "tables": ["run_manifests", "job_manifests"],
        "producer": "every job (crawl, crawl-retry, merge, enrich)",
        "consumers": ["dbt (run_completeness)", "merge", "enrichment job", "egress-check"],
    },
    "enrichment": {
        "tables": ["enrichments"],
        "producer": "post-load enrichment job (enrich-week)",
        "consumers": ["dbt (stg_enrichments, dim_coffee)"],
    },
}


def generate(name: str, version: str) -> dict:
    spec = CONTRACTS[name]
    tables = {t: [{"name": f.name, "type": str(f.type), "nullable": f.nullable,
                   "description": contract_docs.COLUMNS[t][f.name]} for f in SCHEMAS[t]]
              for t in spec["tables"]}  # fmt: skip
    body = {
        "descriptions": {t: contract_docs.TABLES[t] for t in spec["tables"]},
        "tables": tables,
        "files": spec.get("files", {}),
        "quality": contract_docs.QUALITY[name],
    } | ({"values": spec["values"]} if "values" in spec else {})
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return {"contract": name, "version": version, "fingerprint": fingerprint, "producer": spec["producer"],
            "consumers": spec["consumers"], **body}  # fmt: skip


def path(name: str) -> Path:
    return DIR / f"{name}.contract.json"


def load(name: str) -> dict:
    return json.loads(path(name).read_text())


if __name__ == "__main__":
    if sys.argv[1:2] == ["--write"]:
        name, version = sys.argv[2], sys.argv[3]
        DIR.mkdir(exist_ok=True)
        path(name).write_text(json.dumps(generate(name, version), indent=1) + "\n")
        print(path(name))
