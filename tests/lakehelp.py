"""Build small synthetic lakes for tests: a few roasters, a few weekly runs, chosen failures."""

from datetime import UTC, date, datetime

from coffeedata.lake import Lake

_n = [0]


def batch() -> str:
    _n[0] += 1
    return f"t{_n[0]:04d}"


def at(d: str, hour: int = 12) -> datetime:
    return datetime.fromisoformat(d).replace(hour=hour, tzinfo=UTC)


def product(
    pid: str, price: float | None = 20.0, available: bool = True, coffee: bool = True, title: str = ""
) -> dict:
    return {
        "product_id": pid,
        "handle": f"h{pid}",
        "url": f"https://x/{pid}",
        # The build classifies from the title, so the default title has to read as coffee (or not).
        "title": title or (f"Coffee {pid} Roast" if coffee else f"Mug {pid}"),
        "platform": "shopify",
        "tags": [],
        "body_hash": f"b{pid}",
        "is_coffee": coffee,
        "variants": [
            {
                "variant_id": f"{pid}-v",
                "title": "12 oz",
                "price_usd": price,
                "grams": 340,
                "available": available,
            }
        ],
    }


def run(lake: Lake, run_date: str, roaster: str, products: list[dict] | None, hour: int = 12) -> str:
    """Record one fetch attempt: products=None means the fetch failed."""
    b = batch()
    fetched = at(run_date, hour)
    lake.write_partition(
        "fetches",
        run_date,
        b,
        [
            {
                "run_date": date.fromisoformat(run_date),
                "batch_id": b,
                "fetched_at": fetched,
                "roaster_id": roaster,
                "adapter": "shopify",
                "status": "error" if products is None else "ok",
                "error": "boom" if products is None else None,
                "products": len(products or []),
                "coffees": sum(p["is_coffee"] for p in products or []),
            }
        ],
    )
    if products:
        lake.write_partition(
            "snapshots",
            run_date,
            b,
            [
                p
                | {
                    "run_date": date.fromisoformat(run_date),
                    "batch_id": b,
                    "fetched_at": fetched,
                    "roaster_id": roaster,
                }
                for p in products
            ],
        )
    return b


def probe(lake: Lake, run_date: str, roaster: str, status: str = "ok", platform: str = "shopify") -> None:
    b = batch()
    lake.write_partition(
        "probes",
        run_date,
        b,
        [
            {
                "run_date": date.fromisoformat(run_date),
                "batch_id": b,
                "probed_at": at(run_date, 6),
                "roaster_id": roaster,
                "status": status,
                "final_host": roaster if status == "ok" else "",
                "platform": platform if status == "ok" else "",
            }
        ],
    )


# The shots schema before the app's columns, as files written by older versions have it.
OLD_SHOT_COLUMNS = [
    "shot_id",
    "logged_at",
    "deleted",
    "brewed_at",
    "coffee_id",
    "coffee_freetext",
    "roast_date",
    "grinder_setting",
    "dose_g",
    "yield_g",
    "time_s",
    "water_temp_c",
    "brew_method",
    "rating",
    "taste",
    "is_milk_drink",
    "notes",
]


def old_shot(lake: Lake, shot_id: str, brewed: str, **fields) -> None:
    """Write a shot file exactly as the pre-app code did: v1 columns only, v1 schema file beside it."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from coffeedata.lake import SCHEMAS

    schema = pa.schema([SCHEMAS["shots"].field(c) for c in OLD_SHOT_COLUMNS])
    v1 = lake.root / "shots/shot_id=schema.parquet"
    if not v1.exists():
        pq.write_table(schema.empty_table(), v1)
    row = {
        "shot_id": shot_id,
        "logged_at": at(brewed),
        "deleted": False,
        "brewed_at": at(brewed, 7),
        "grinder_setting": 12.0,
        "dose_g": 18.0,
        "yield_g": 36.0,
        "time_s": 28.0,
        "brew_method": "espresso",
        "rating": 3,
        "is_milk_drink": False,
    } | fields
    pq.write_table(
        pa.Table.from_pylist([{c: row.get(c) for c in OLD_SHOT_COLUMNS}], schema=schema),
        lake.root / f"shots/{shot_id}__{brewed.replace('-', '')}T120000.parquet",
    )


def old_file(root, table: str, rel: str, missing: list[str], *rows: dict) -> None:
    """Write a file under the lake `root` as older code did: without the columns in `missing`, which it didn't
    know about yet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from coffeedata.lake import SCHEMAS

    schema = pa.schema([f for f in SCHEMAS[table] if f.name not in missing])
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    table_ = pa.Table.from_pylist([{f.name: r.get(f.name) for f in schema} for r in rows], schema=schema)
    pq.write_table(table_, path)


def manifest(lake: Lake, run_date: str, roasters: list[str], mode: str = "full") -> None:
    """Record that an ingest of `run_date` set out to fetch `roasters` (what ingest.run writes first)."""
    b = batch()
    lake.write_partition("run_manifests", run_date, b, [
        {"run_date": date.fromisoformat(run_date), "manifest_id": b, "started_at": at(run_date, 6),
         "roaster_id": r, "status": "active", "adapter": "shopify", "mode": mode} for r in roasters])  # fmt: skip
