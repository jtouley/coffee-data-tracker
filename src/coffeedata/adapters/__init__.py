"""Catalog adapters, one per storefront platform. Each exposes fetch(client, roaster) -> list[CatalogItem]."""

from __future__ import annotations

from collections.abc import Callable

from ..http import PoliteClient
from ..models import CatalogItem, Roaster
from . import generic, shopify, squarespace, woocommerce

Adapter = Callable[[PoliteClient, Roaster], list[CatalogItem]]

ADAPTERS: dict[str, Adapter] = {
    "shopify": shopify.fetch,
    "woocommerce": woocommerce.fetch,
    "squarespace": squarespace.fetch,
    "generic": generic.fetch,
}


def fetch_catalog(client: PoliteClient, roaster: Roaster) -> list[CatalogItem]:
    return ADAPTERS[roaster.adapter](client, roaster)
