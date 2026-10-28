import httpx
import pytest
import respx

from coffeedata.adapters import generic, shopify, square_online, squarespace, woocommerce
from coffeedata.http import Blocked, FetchError, PoliteClient
from coffeedata.models import Roaster

from .conftest import FIX, load


def _r(host="x.com", platform="shopify"):
    return Roaster(roaster_id=host, name="R", host=host, platform=platform)


def test_shopify_parse(fixture_items):
    items = fixture_items["counterculturecoffee.com"]
    assert len(items) == 12
    first = items[0]
    assert first.platform == "shopify" and first.url.startswith("https://counterculturecoffee.com/products/")
    assert first.coffee_id == f"counterculturecoffee.com:{first.product_id}"
    assert first.body_hash and first.variants and first.variants[0].price_usd


def test_shopify_tolerates_odd_payloads():
    p = {
        "id": 1,
        "title": "Kenya",
        "images": ["https://img/x.jpg"],
        "tags": "a, b",
        "variants": [{"id": 2, "title": "12 oz", "price": "20.00", "grams": "oops"}],
    }
    item = shopify.parse_product(p, _r())
    assert item.image == "https://img/x.jpg" and item.tags == ["a", "b"] and item.variants[0].grams == 340


def test_squarespace_parse(fixture_items):
    items = fixture_items["vividcoffee.com"]
    assert any(v.grams for i in items for v in i.variants)
    assert all(i.url.startswith("https://vividcoffee.com/") for i in items)


def test_generic_jsonld():
    products = generic.jsonld_products((FIX / "wix_product_jsonld.html").read_text())
    assert len(products) == 1
    item = generic.parse_jsonld(
        products[0], "https://www.1802roasters.com/product-page/x", _r("1802roasters.com")
    )
    assert item.title and item.variants


def test_generic_ignores_malformed_offers():
    item = generic.parse_jsonld(
        {"name": "X", "offers": ["junk", {"price": "12", "name": "12 oz"}]}, "u", _r()
    )
    assert [v.price_usd for v in item.variants] == [12.0]


def test_generic_repeated_sku_gives_unique_variant_ids():
    # Seen live (ikoncoffee.com): every offer carries the product's SKU, which duplicated price events.
    offers = [{"sku": "WB-12oz", "name": n, "price": "18"} for n in ("Whole bean", "Drip", "Espresso")]
    item = generic.parse_jsonld({"name": "X", "offers": offers}, "u", _r())
    assert [v.variant_id for v in item.variants] == ["WB-12oz", "WB-12oz#1", "WB-12oz#2"]


def test_product_urls_rank_beans_before_merch():
    urls = [
        "https://x.com/product-page/logo-mug",
        "https://x.com/product-page/ethiopia-guji",
        "https://x.com/about",
        "https://x.com/product/house-blend/ABCDEFG123",
    ]
    ranked = generic.product_urls(urls)
    assert ranked[-1].endswith("logo-mug") and "https://x.com/about" not in ranked


# --- failures must raise, never look like an empty catalog ------------------------------------------


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        yield m


def test_shopify_403_raises(mock):
    mock.get("https://x.com/products.json").mock(return_value=httpx.Response(403, text="challenge"))
    with PoliteClient(min_interval=0, retries=1) as c, pytest.raises(FetchError, match="403"):
        shopify.fetch(c, _r())


def test_shopify_later_page_failure_raises(mock):
    full = {"products": [{"id": i, "title": f"P{i}", "handle": f"p{i}", "variants": []} for i in range(250)]}
    mock.get("https://x.com/products.json", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=full)
    )
    mock.get("https://x.com/products.json", params={"page": "2"}).mock(return_value=httpx.Response(502))
    with PoliteClient(min_interval=0, retries=1) as c, pytest.raises(FetchError):
        shopify.fetch(c, _r())


def _page(ids):
    return httpx.Response(
        200, json={"products": [{"id": i, "title": f"P{i}", "handle": f"p{i}", "variants": []} for i in ids]}
    )


def test_shopify_short_pages_are_not_the_end(mock):
    # onyxcoffeelab.com: 249, 247, 234 and 242 products on its four pages; we used to stop after the first.
    pages = {"1": range(0, 249), "2": range(250, 497), "3": [], "4": range(900, 910)}
    for n, ids in pages.items():
        mock.get("https://x.com/products.json", params={"page": n}).mock(return_value=_page(ids))
    with PoliteClient(min_interval=0) as c:
        assert len(shopify.fetch(c, _r())) == 249 + 247  # an empty page ends it


def test_shopify_store_that_ignores_page_stops(mock):
    route = mock.get("https://x.com/products.json").mock(return_value=_page(range(10)))
    with PoliteClient(min_interval=0) as c:
        assert len(shopify.fetch(c, _r())) == 10
    assert route.call_count == 2


def test_shopify_empty_catalog_is_an_answer(mock):
    mock.get("https://x.com/products.json").mock(return_value=httpx.Response(200, json={"products": []}))
    with PoliteClient(min_interval=0) as c:
        assert shopify.fetch(c, _r()) == []


@respx.mock
def test_robots_disallow_and_401_robots():
    mock = respx
    mock.get("https://y.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /products.json\n")
    )
    mock.get("https://z.com/robots.txt").mock(return_value=httpx.Response(401))
    route = mock.get(url__regex=r"https://[yz].com/products.json").mock(
        return_value=httpx.Response(200, json={})
    )
    with PoliteClient(min_interval=0) as c:
        for host in ("y.com", "z.com"):
            with pytest.raises(Blocked):
                shopify.fetch(c, _r(host))
    assert route.call_count == 0


def test_woocommerce_variation_prices(mock):
    p = {
        "id": 10,
        "name": "Ethiopia",
        "slug": "ethiopia",
        "is_in_stock": True,
        "prices": {
            "price": "1800",
            "currency_minor_unit": 2,
            "price_range": {"min_amount": "1800", "max_amount": "7000"},
        },
        "variations": [
            {"id": 11, "attributes": [{"name": "Size", "value": "12 oz"}]},
            {"id": 12, "attributes": [{"name": "Size", "value": "5 lb"}]},
        ],
    }
    base = "https://x.com/wp-json/wc/store/v1/products"
    mock.get(base).mock(return_value=httpx.Response(200, json=[p]))
    mock.get(f"{base}/11").mock(
        return_value=httpx.Response(200, json={"prices": {"price": "1800", "currency_minor_unit": 2}})
    )
    mock.get(f"{base}/12").mock(
        return_value=httpx.Response(200, json={"prices": {"price": "7000", "currency_minor_unit": 2}})
    )
    with PoliteClient(min_interval=0) as c:
        [item] = woocommerce.fetch(c, _r(platform="woocommerce"))
    by_size = {v.grams: v.price_usd for v in item.variants}
    assert by_size == {340: 18.0, 2268: 70.0}  # not 18.0 for both


def test_woocommerce_fixture_fetch(mock, roasters):
    mock.get("https://anaheimcoffee.com/wp-json/wc/store/v1/products").mock(
        return_value=httpx.Response(200, json=load("woocommerce_anaheimcoffee.json"))
    )
    mock.get(url__regex=r"https://anaheimcoffee.com/wp-json/wc/store/v1/products/\d+").mock(
        return_value=httpx.Response(200, json={"prices": {"price": "2000", "currency_minor_unit": 2}})
    )
    with PoliteClient(min_interval=0) as c:
        items = woocommerce.fetch(c, roasters["anaheimcoffee.com"])
    assert len(items) == 8 and all(1 < (v.price_usd or 2) < 500 for i in items for v in i.variants)


def test_squarespace_fixture_fetch(mock, roasters):
    mock.get("https://vividcoffee.com/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="<urlset><url><loc>https://vividcoffee.com/shop/a</loc></url>"
            "<url><loc>https://vividcoffee.com/shop/b</loc></url></urlset>",
        )
    )
    mock.get("https://vividcoffee.com/shop").mock(
        return_value=httpx.Response(200, json=load("squarespace_vividcoffee.json"))
    )
    with PoliteClient(min_interval=0) as c:
        assert len(squarespace.fetch(c, roasters["vividcoffee.com"])) == 8


def test_square_online(mock):
    html = '<script>{"user":{"id":143063903,"properties":{}},"site_id":"480502767511180931"}</script>'
    mock.get("https://sq.com/").mock(return_value=httpx.Response(200, text=html))
    product = {
        "id": "ABC",
        "site_product_id": "142",
        "name": "Ethiopia Guji 12oz",
        "site_link": "product/ethiopia/142",
        "absolute_site_link": "https://sq.com/product/ethiopia/142",
        "badges": {"out_of_stock": False},
        "price": {"regular_low": 18, "regular_high": 18},
        "short_description": "<p>Washed. Notes: peach</p>",
        "images": {"data": [{"absolute_url": "https://img/1.jpg"}]},
    }
    mock.get(
        url__regex=r"https://cdn5.editmysite.com/app/store/api/v28/editor/users/143063903/sites/480502767511180931/products.*"
    ).mock(
        return_value=httpx.Response(200, json={"data": [product], "meta": {"pagination": {"total_pages": 1}}})
    )
    with PoliteClient(min_interval=0) as c:
        [item] = square_online.fetch(c, _r("sq.com", "square_online?"))
    assert item.product_id == "142" and item.handle == "ethiopia" and item.variants[0].price_usd == 18
    assert item.variants[0].grams == 340 and item.variants[0].available is True and "Washed" in item.body_text


def test_square_online_price_range_is_not_a_price():
    p = {"id": "A", "name": "Blend", "price": {"regular_low": 18, "regular_high": 60}}
    assert square_online.parse_product(p, _r()).variants[0].price_usd is None


def test_generic_mostly_failing_raises(mock):
    mock.get("https://x.com/sitemap.xml").mock(
        return_value=httpx.Response(
            200,
            text="<urlset>"
            + "".join(f"<url><loc>https://x.com/products/p{i}</loc></url>" for i in range(3))
            + "</urlset>",
        )
    )
    mock.get(url__regex=r"https://x.com/products/p\d").mock(return_value=httpx.Response(500))
    with PoliteClient(min_interval=0, retries=1) as c, pytest.raises(FetchError):
        generic.fetch(c, _r(platform="other"))


@pytest.mark.parametrize(
    "html, ids",
    [
        (
            '<script>{"user":{"id":143063903},"site_id":"480502767511180931"}</script>',
            ("143063903", "480502767511180931"),
        ),
        (
            '<script>com_userID = "91999742"; com_currentSite = "365034632528666450";</script>',
            ("91999742", "365034632528666450"),
        ),
        (
            "<script>com_userID = '91999742'; com_currentSite = '365034632528666450';</script>",
            ("91999742", "365034632528666450"),
        ),
        ('<a href="https://app.squareup.com/gift/ML0CVF31XSMZQ/order">Gift cards</a>', None),
    ],
)
def test_square_online_site_ids(html, ids):
    assert square_online.site_ids(html) == ids


def test_square_online_upload_path_ids_are_separate():
    html = '<div style="background: url(//www.weebly.com/uploads/b/120598593-592781055177749699/a.jpg)">'
    assert square_online.site_ids(html) is None
    assert square_online.upload_ids(html) == ("120598593", "592781055177749699")


def test_square_online_store_links_only_match_square_site_hosts():
    html = (
        '<a href="https://shop.square.site.evil.com/">x</a> <a href="https://Airship-Coffee.square.site/#T7">a</a>'
        ' <a href="https://airship-coffee.square.site/menu">b</a> <a href="https://other.square.site">c</a>'
    )
    assert square_online.store_links(html) == [
        "https://airship-coffee.square.site",
        "https://other.square.site",
    ]


def test_square_online_follows_a_linked_square_site_store(mock):
    home = '<a href="https://airship-coffee.square.site/#T72CBWSPVBS4I3FRZUACDA7W">Shop</a>'
    mock.get("https://sq.com/").mock(return_value=httpx.Response(200, text=home))
    mock.get("https://airship-coffee.square.site/").mock(
        return_value=httpx.Response(200, text='{"user":{"id":130995357},"site_id":804583245133429738}')
    )
    mock.get(
        url__regex=r"https://cdn5.editmysite.com/.*/users/130995357/sites/804583245133429738/products.*"
    ).mock(return_value=httpx.Response(200, json={"data": [{"id": "A", "name": "Kenya 12oz"}], "meta": {}}))
    with PoliteClient(min_interval=0) as c:
        [item] = square_online.fetch(c, _r("sq.com", "square_online?"))
    assert item.title == "Kenya 12oz"


def test_square_online_without_a_store_fails(mock):
    mock.get("https://sq.com/").mock(return_value=httpx.Response(200, text="<p>Order on Square</p>"))
    with PoliteClient(min_interval=0) as c, pytest.raises(FetchError, match="no Square Online store ids"):
        square_online.fetch(c, _r("sq.com", "square_online?"))


def test_square_online_tries_each_linked_store_and_links_products_to_it(mock):
    home = (
        '<img src="//www.weebly.com/uploads/b/111111111-222222222222222222/borrowed.jpg">'
        '<a href="https://gone.square.site/">old</a> <a href="https://shop-x.square.site/">shop</a>'
    )
    mock.get("https://sq.com/").mock(return_value=httpx.Response(200, text=home))
    mock.get("https://gone.square.site/").mock(return_value=httpx.Response(404))
    mock.get("https://shop-x.square.site/").mock(
        return_value=httpx.Response(200, text='{"user":{"id":130995357},"site_id":804583245133429738}')
    )
    mock.get(
        url__regex=r"https://cdn5.editmysite.com/.*/users/130995357/sites/804583245133429738/products.*"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "A", "name": "Kenya", "site_link": "product/kenya/7"}]}
        )
    )
    with PoliteClient(min_interval=0) as c:
        [item] = square_online.fetch(c, _r("sq.com", "square_online?"))
    assert item.url == "https://shop-x.square.site/product/kenya/7"  # the store's address, not the roaster's
