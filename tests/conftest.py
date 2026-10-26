import json
from pathlib import Path

import pytest

from coffeedata import registry
from coffeedata.adapters import shopify, squarespace, woocommerce
from coffeedata.models import Roaster

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def roasters() -> dict[str, Roaster]:
    return {r.slug: r for r in registry.roasters(registry.load(FIX / "roasters.csv"))}


@pytest.fixture
def fixture_items(roasters):
    """CatalogItems parsed from every recorded storefront response, keyed by roaster slug."""
    out = {}
    for slug in ("counterculturecoffee", "onyxcoffeelab", "intelligentsia"):
        out[slug] = [shopify.parse_product(p, roasters[slug]) for p in load(f"shopify_{slug}.json")["products"]]
    out["anaheimcoffee"] = [woocommerce.parse_product(p, roasters["anaheimcoffee"])
                            for p in load("woocommerce_anaheimcoffee.json")]
    out["vividcoffee"] = [squarespace.parse_item(i, roasters["vividcoffee"])
                          for i in load("squarespace_vividcoffee.json")["items"]]
    return out
