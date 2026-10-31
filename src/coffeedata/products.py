"""One product version's derived facts: coffee or not, and its rule-based attributes. ELT: computed after load.

A product version is everything the classifier and the extraction rules read (title, type, tags, bag sizes,
description), keyed by input_hash (verdict.INPUT_HASH; dbt's input_hash macro). The dbt model stg_products, the
roaster status in registry.STATE_SQL, and the post-load enrichment step (enrich.pending) all call `derive`, so the
build, the crawl's roaster selection and the Claude candidates agree by construction.
"""

from __future__ import annotations

from . import classify, extract
from .models import CatalogItem, Enrichment, Variant

# The attributes stg_products publishes, in its schema's order.
ATTRS = [
    "origin_country",
    "origin_region",
    "producer",
    "process",
    "varietals",
    "elevation_masl_min",
    "elevation_masl_max",
    "roast_level",
    "tasting_notes",
    "is_blend",
    "is_decaf",
]


def item(r: dict) -> CatalogItem:
    """A product version row (title, product_type, tags, grams, body_hash, body_text) as a CatalogItem."""
    return CatalogItem(
        roaster_id="",
        product_id="",
        handle="",
        url="",
        title=r.get("title") or "",
        platform="",
        product_type=r.get("product_type") or "",
        tags=r.get("tags") or [],
        variants=[Variant(variant_id=str(i), grams=g) for i, g in enumerate(r.get("grams") or [])],
        body_hash=r.get("body_hash") or "",
        body_text=r.get("body_text") or "",
    )


def derive(r: dict, verdicts: dict[str, str]) -> tuple[dict, Enrichment | None]:
    """(stg_products' row, the rules' Enrichment when it is coffee). A stored Claude verdict for the version decides
    coffee or not, except that the rules' outright exclusions always hold."""
    it = item(r)
    label = verdicts.get(r["input_hash"])
    by_claude = label in ("coffee", "not_coffee") and not classify.excluded(it)
    out = {
        "input_hash": r["input_hash"],
        "is_coffee": label == "coffee" if by_claude else classify.is_coffee(it),
        "classified_by": "claude" if by_claude else "rules",
        "is_wholesale": classify.is_wholesale(it),
        "clean_title": it.title,
    }
    e = extract.rules(it) if out["is_coffee"] else None
    return out | {a: getattr(e, a) if e else None for a in ATTRS}, e


def verdicts(lake, model_id: str | None, definition: str) -> dict[str, str]:
    """stg_verdicts in Python: the latest verdict per version for this model and definition (none: rules only)."""
    if not model_id:
        return {}
    rows = (
        lake.connect()
        .execute(
            f"""select input_hash, arg_max(label, decided_at) from {lake.scan("verdicts")}
                where model = ? and definition = ? group by 1""",
            [model_id, definition],
        )
        .fetchall()
    )
    return dict(rows)
