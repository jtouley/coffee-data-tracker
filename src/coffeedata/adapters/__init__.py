"""Catalog adapters, one per storefront platform. Each exposes fetch(client, roaster) -> list[CatalogItem]."""

from __future__ import annotations

from collections.abc import Callable

from ..http import PoliteClient
from ..models import CatalogItem, Roaster
from . import generic, shopify, square_online, squarespace, woocommerce

Adapter = Callable[[PoliteClient, Roaster], list[CatalogItem]]

ADAPTERS: dict[str, Adapter] = {
    "shopify": shopify.fetch,
    "woocommerce": woocommerce.fetch,
    "squarespace": squarespace.fetch,
    "square_online": square_online.fetch,
    "generic": generic.fetch,
}


def fetch_catalog(
    client: PoliteClient,
    roaster: Roaster,
    resume: dict | None = None,
    known_products: int | None = None,
    identity: shopify.Identity | None = None,
) -> list[CatalogItem]:
    """Shopify only: resume, a catalog's progress when it was blocked mid-way (shopify.PageBlocked); known_products,
    the store's product count at its latest ok fetch (lets a short page end the catalog); identity, the ids the
    generic adapter stored a store's products under, which they keep when it is read from /products.json
    (shopify.parse_product)."""
    # The adapter, not the hostname, says which platform's shared rate limit (if any) the requests count against.
    view = client.for_platform(roaster.adapter)
    extra = resume is not None or known_products is not None or bool(identity)
    if roaster.adapter == "shopify" and extra:
        return shopify.fetch(view, roaster, resume=resume, known_products=known_products, identity=identity)
    if roaster.adapter == "generic" and extra:
        # a Shopify storefront the generic crawl reads from /products.json (generic.fetch) resumes the same way
        return generic.fetch(view, roaster, resume=resume, known_products=known_products, identity=identity)
    return ADAPTERS[roaster.adapter](view, roaster)
