import csv
import json
from pathlib import Path

import pytest

from coffeedata.adapters import shopify, squarespace, woocommerce
from coffeedata.lake import Lake
from coffeedata.models import Roaster

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def roasters() -> dict[str, Roaster]:
    with (FIX / "roasters.csv").open() as f:
        return {r["roaster_id"]: Roaster(roaster_id=r["roaster_id"], name=r["name"], host=r["final_host"],
                                         platform=r["platform"]) for r in csv.DictReader(f)}


@pytest.fixture
def lake(tmp_path, monkeypatch) -> Lake:
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake"))
    return Lake()


@pytest.fixture
def fixture_items(roasters):
    """CatalogItems parsed from every recorded storefront response, keyed by roaster id."""
    out = {}
    for rid in ("counterculturecoffee.com", "onyxcoffeelab.com", "intelligentsia.com"):
        name = rid.removesuffix(".com")
        out[rid] = [shopify.parse_product(p, roasters[rid]) for p in load(f"shopify_{name}.json")["products"]]
    out["anaheimcoffee.com"] = [woocommerce.parse_product(p, roasters["anaheimcoffee.com"])
                                for p in load("woocommerce_anaheimcoffee.json")]
    out["vividcoffee.com"] = [squarespace.parse_item(i, roasters["vividcoffee.com"])
                              for i in load("squarespace_vividcoffee.json")["items"]]
    return out
