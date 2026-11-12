"""The importer rules' words and patterns (importer_rules.vocabulary(): its own, and the catalog's process, variety,
country and region vocabularies from extract.py and origins.py), as a table the SQL rules read. Calling the package,
the way stg_products.py calls the catalog's rules, keeps one copy of each vocabulary."""

import pyarrow as pa

from coffeedata import importer_rules

SCHEMA = pa.schema(
    [
        ("kind", pa.string()),
        ("term", pa.string()),
        ("value", pa.string()),
        ("value2", pa.string()),
        ("ord", pa.int64()),
    ]
)


def model(dbt, session):
    dbt.config(materialized="table")
    return pa.Table.from_pylist(importer_rules.vocabulary(), schema=SCHEMA)
