"""The fields each lot's page states (importers.stated: lot ID, name, origin, region, process, status, estimated
arrival, warehouse, bags, bag size and weight), read from its stored cells by the same code the weekly snapshot uses
to check a page, so there is one reading of them. `problems` lists the cells that read as null."""

import pyarrow as pa

from coffeedata import importers

FIELDS = [
    ("lot_id", pa.string()),
    ("name", pa.string()),
    ("origin", pa.string()),
    ("region", pa.string()),
    ("process", pa.string()),
    ("status", pa.string()),
    ("est_arrival", pa.string()),
    ("warehouse", pa.string()),
    ("bags", pa.int64()),
    ("bag_size", pa.string()),
    ("bag_kg", pa.float64()),
]
SCHEMA = pa.schema(
    [
        ("week", pa.date32()),
        ("importer", pa.string()),
        ("row", pa.int32()),
        *FIELDS,
        ("problems", pa.list_(pa.string())),
    ]
)


def row(lot: dict) -> dict:
    cells = {c["column"]: c["text"] for c in lot["cells"] or []}
    problems: list[str] = []
    fields = importers.stated(lot["importer"], cells, problems)
    return (
        {k: lot[k] for k in ("week", "importer", "row")}
        | {k: fields[k] for k, _ in FIELDS}
        | {"problems": problems}
    )


def model(dbt, session):
    dbt.config(materialized="table")
    lots = dbt.ref("stg_importer_lots").to_arrow_table().select(["week", "importer", "row", "cells"])
    return pa.Table.from_pylist([row(r) for r in lots.to_pylist()], schema=SCHEMA)
