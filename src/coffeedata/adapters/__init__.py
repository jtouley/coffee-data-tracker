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


def fetch_catalog(client: PoliteClient, roaster: Roaster, resume: dict | None = None) -> list[CatalogItem]:
    """resume: a Shopify catalog's progress when it was blocked mid-way (shopify.PageBlocked): its next page."""
    # The adapter, not the hostname, says which platform's shared rate limit (if any) the requests count against.
    view = client.for_platform(roaster.adapter)
    if resume is not None and roaster.adapter == "shopify":
        return shopify.fetch(view, roaster, resume=resume)
    return ADAPTERS[roaster.adapter](view, roaster)
