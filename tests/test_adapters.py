import httpx
import respx

from coffeedata.adapters import generic, shopify, squarespace, woocommerce
from coffeedata.http import PoliteClient

from .conftest import FIX, load


def test_shopify_parse(fixture_items):
    items = fixture_items["counterculturecoffee"]
    assert len(items) == 12
    first = items[0]
    assert first.platform == "shopify" and first.url.startswith("https://counterculturecoffee.com/products/")
    assert first.body_hash and first.variants and first.variants[0].price_usd


def test_woocommerce_prices_are_in_major_units(fixture_items):
    items = fixture_items["anaheimcoffee"]
    prices = [v.price_usd for i in items for v in i.variants if v.price_usd]
    assert prices and all(1 < p < 500 for p in prices)


def test_squarespace_parse(fixture_items):
    items = fixture_items["vividcoffee"]
    assert any(v.grams for i in items for v in i.variants)
    assert all(i.url.startswith("https://vividcoffee.com/") for i in items)


def test_generic_jsonld():
    html = (FIX / "wix_product_jsonld.html").read_text()
    products = generic.jsonld_products(html)
    assert len(products) == 1
    item = generic.parse_jsonld(products[0], "https://www.1802roasters.com/product-page/x", _roaster())
    assert item.title and item.variants


def test_product_urls_rank_beans_before_merch():
    urls = ["https://x.com/product-page/logo-mug", "https://x.com/product-page/ethiopia-guji",
            "https://x.com/about", "https://x.com/product/house-blend/ABCDEFG123"]
    ranked = generic.product_urls(urls)
    assert ranked[-1].endswith("logo-mug") and "https://x.com/about" not in ranked


def _roaster():
    from coffeedata.models import Roaster
    return Roaster(slug="r", name="R", domain="x.com")


@respx.mock
def test_shopify_fetch_paginates_until_short_page(roasters):
    page = load("shopify_onyxcoffeelab.json")
    respx.get("https://onyxcoffeelab.com/robots.txt").mock(return_value=httpx.Response(404))
    route = respx.get("https://onyxcoffeelab.com/products.json").mock(return_value=httpx.Response(200, json=page))
    with PoliteClient(min_interval=0) as c:
        items = shopify.fetch(c, roasters["onyxcoffeelab"])
    assert len(items) == len(page["products"]) and route.call_count == 1


@respx.mock
def test_robots_disallow_is_honored(roasters):
    respx.get("https://onyxcoffeelab.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /products.json\n"))
    route = respx.get("https://onyxcoffeelab.com/products.json").mock(return_value=httpx.Response(200, json={}))
    with PoliteClient(min_interval=0) as c:
        try:
            shopify.fetch(c, roasters["onyxcoffeelab"])
        except Exception as e:  # noqa: BLE001
            assert type(e).__name__ == "Blocked"
    assert route.call_count == 0


@respx.mock
def test_woocommerce_and_squarespace_fetch(roasters):
    respx.get(url__regex=r"https://.*/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://anaheimcoffee.com/wp-json/wc/store/v1/products").mock(
        return_value=httpx.Response(200, json=load("woocommerce_anaheimcoffee.json")))
    respx.get("https://vividcoffee.com/sitemap.xml").mock(return_value=httpx.Response(
        200, text="<urlset><url><loc>https://vividcoffee.com/shop/a</loc></url>"
                  "<url><loc>https://vividcoffee.com/shop/b</loc></url></urlset>"))
    respx.get("https://vividcoffee.com/shop").mock(
        return_value=httpx.Response(200, json=load("squarespace_vividcoffee.json")))
    with PoliteClient(min_interval=0) as c:
        assert len(woocommerce.fetch(c, roasters["anaheimcoffee"])) == 8
        assert len(squarespace.fetch(c, roasters["vividcoffee"])) == 8
