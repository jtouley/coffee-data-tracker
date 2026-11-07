"""The build's view of a product version, shared so the build, the enrichment job and the registry decide alike.

dbt's stg_products runs these over stg_product_inputs on every build; the post-load enrichment job (enrich.py)
runs them over raw snapshots to pick Claude's candidates; registry.STATE_SQL uses them to tell an active roaster
from one with no coffee. Nothing at crawl time classifies or extracts (ELT): ingestion only loads raw rows.
"""

from __future__ import annotations

from . import classify, extract
from .models import CatalogItem, Enrichment, Variant

# The product-version key: everything the classifier and the extraction rules read. The same expression as
# dbt's product_input_hash macro (dbt/macros/products.sql), over a snapshots row aliased `s`.
INPUT_HASH_SQL = (
    "md5(to_json(struct_pack({s}.title, {s}.product_type, {s}.tags, {s}.body_hash, "
    "grams := list_transform({s}.variants, lambda v: v.grams))))"
)


def input_hash_sql(alias: str = "s") -> str:
    return INPUT_HASH_SQL.format(s=alias)


def item(title, product_type, tags, grams, body_hash, body_text) -> CatalogItem:
    return CatalogItem(
        roaster_id="",
        product_id="",
        handle="",
        url="",
        title=title or "",
        platform="",
        product_type=product_type or "",
        tags=tags or [],
        variants=[Variant(variant_id=str(i), grams=g) for i, g in enumerate(grams or [])],
        body_hash=body_hash or "",
        body_text=body_text or "",
    )


def decide(it: CatalogItem, label: str | None) -> tuple[bool, str]:
    """(is_coffee, classified_by): Claude's stored verdict where there is one, unless the rules exclude the
    product outright; otherwise the rules."""
    is_coffee, by, _ = decide_why(it, label)
    return is_coffee, by


def decide_why(it: CatalogItem, label: str | None) -> tuple[bool, str, str]:
    """decide's answer and why: verdict:<label> when Claude's verdict decided, else the rule that did
    (classify.decision: excluded:<rule>, a named rule, or signals:<n>). The reason only explains the answer."""
    exclusion = classify.exclusion(it)
    if label in ("coffee", "not_coffee") and exclusion is None:
        return label == "coffee", "claude", f"verdict:{label}"
    is_coffee, why = (False, f"excluded:{exclusion}") if exclusion else classify.decision(it)
    return is_coffee, "rules", why


def rules(it: CatalogItem, label: str | None) -> Enrichment | None:
    """The rule-based attributes of a coffee (None if it isn't one): what stg_products shows, and the base Claude
    enrichment builds on."""
    return extract.rules(it) if decide(it, label)[0] else None


def needs_claude(it: CatalogItem, label: str | None) -> Enrichment | None:
    """The rules' attributes when they leave a coffee incompletely described (extract.needs_llm), else None."""
    e = rules(it, label)
    return e if e is not None and extract.needs_llm(e) else None
