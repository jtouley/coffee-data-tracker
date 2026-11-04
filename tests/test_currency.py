"""Each price's currency as the store states it (raw 1.6.0, snapshots.variants.currency), and the public tables'
USD-only, US-only rule (stg_snapshots, public_roaster, tests/public_prices_are_usd.sql).

Store shapes: aromacoffee.co.za's Store API states `currency_code: ZAR` (checked 2026-11-04), lacabra.com's
products.json sets cart_currency=DKK, a Square Online page embeds storeInfo.currency, Squarespace's priceMoney and
schema.org's priceCurrency carry it per price."""

import csv
import shutil
from datetime import date
from pathlib import Path

import duckdb
import httpx
import pytest
import respx

from coffeedata import lake as lake_mod
from coffeedata.adapters import generic, shopify, square_online, squarespace, woocommerce
from coffeedata.http import PoliteClient
from coffeedata.models import CatalogItem, Roaster, Variant, currency_code

from .conftest import FIX, load
from .lakehelp import probe, product, run


def _r(host="x.com", platform="shopify", rid=None):
    return Roaster(roaster_id=rid or host, name="X", host=host, platform=platform)


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        yield m


# --- the raw value, as stated ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, stated",
    [("USD", "USD"), ("ZAR", "ZAR"), ("usd", "usd"), ("", None), ("  ", None), (None, None), (840, None)],
)
def test_a_currency_is_kept_as_the_store_states_it(value, stated):
    # Raw values as the store gives them (dbt reads them as ISO 4217); null only when none is stated.
    assert currency_code(value) == stated


def test_variants_default_to_no_stated_currency_and_the_lake_has_the_column():
    assert Variant(variant_id="v").currency is None
    assert "currency" in lake_mod.VARIANT.names
    assert "currency" in CatalogItem.model_fields["variants"].annotation.__args__[0].model_fields  # type: ignore[union-attr]


# --- WooCommerce: prices.currency_code ------------------------------------------------------------


def _woo_simple(pid, price, code, minor=2):
    prices = {"price": str(price), "currency_minor_unit": minor}
    if code is not None:
        prices["currency_code"] = code
    return {"id": pid, "name": f"Coffee {pid} 250g", "slug": f"c{pid}", "prices": prices, "is_in_stock": True}


def test_woocommerce_records_each_prices_currency_code():
    aroma = _r("aromacoffee.co.za", "woocommerce")
    item = woocommerce.parse_product(_woo_simple(1, 16900, "ZAR"), aroma)
    assert [(v.price_usd, v.currency) for v in item.variants] == [(169.0, "ZAR")]
    # A product the Store API gives no currency_code for: none stated, so none recorded.
    assert woocommerce.parse_product(_woo_simple(2, 1800, None), aroma).variants[0].currency is None


def test_woocommerce_fixture_is_usd_everywhere(roasters):
    items = [
        woocommerce.parse_product(p, roasters["anaheimcoffee.com"])
        for p in load("woocommerce_anaheimcoffee.json")
    ]
    assert {v.currency for i in items for v in i.variants} == {"USD"}


def test_woocommerce_sizes_take_their_own_lookups_currency_else_the_listings(mock):
    base = "https://x.com/wp-json/wc/store/v1/products"
    parent = {
        "id": 10, "name": "Kenya", "slug": "kenya", "type": "variable",
        "prices": {"price": "1800", "currency_code": "DKK", "currency_minor_unit": 2,
                   "price_range": {"min_amount": "1800", "max_amount": "3400"}},
        "variations": [{"id": 11, "attributes": [{"name": "Size", "value": "250 g"}]},
                       {"id": 12, "attributes": [{"name": "Size", "value": "1 kg"}]}],
    }  # fmt: skip
    flat = {
        "id": 20, "name": "Blend", "slug": "blend", "type": "variable",
        "prices": {"price": "1500", "currency_code": "DKK", "currency_minor_unit": 2, "price_range": None},
        "variations": [{"id": 21, "attributes": [{"name": "Size", "value": "250 g"}]}],
    }  # fmt: skip

    def variation(vid, price):
        prices = {"price": str(price), "currency_code": "DKK", "currency_minor_unit": 2}
        return {"id": vid, "parent": 10, "prices": prices, "is_in_stock": True}

    def handler(request):
        if request.url.params.get("type") == "variation":
            return httpx.Response(200, json=[variation(11, 1800), variation(12, 3400)])
        return httpx.Response(200, json=[parent, flat], headers={"X-WP-Total": "2"})

    mock.get(base).mock(side_effect=handler)
    with PoliteClient(min_interval=0) as c:
        items = {i.product_id: i for i in woocommerce.fetch(c, _r(platform="woocommerce"))}
    assert [(v.price_usd, v.currency) for v in items["10"].variants] == [(18.0, "DKK"), (34.0, "DKK")]
    assert [(v.price_usd, v.currency) for v in items["20"].variants] == [
        (15.0, "DKK")
    ]  # priced by the listing


def test_woocommerce_one_by_one_lookup_records_its_currency(mock):
    base = "https://x.com/wp-json/wc/store/v1/products"
    one = {"id": 11, "parent": 10, "prices": {"price": "22000", "currency_code": "MXN"}, "is_in_stock": True}
    mock.get(f"{base}/11").mock(return_value=httpx.Response(200, json=one))
    prices: dict = {}
    currencies: dict = {}
    parents = [{"id": 10, "variations": [{"id": 11}]}]
    mock.get(base).mock(return_value=httpx.Response(400))  # the bulk query rejected: each size on its own
    with PoliteClient(min_interval=0, retries=1) as c:
        woocommerce.variation_prices(c, base, "x.com", parents, prices, currencies=currencies)
    assert prices == {"11": 220.0} and currencies == {"11": "MXN"}


# --- Shopify: the page's cart_currency cookie -------------------------------------------------------


def _page(ids, cookie=None):
    body = {"products": [{"id": i, "title": f"P{i}", "handle": f"p{i}", "variants": [{"id": i * 10, "price": "159.00"}]}
                         for i in ids]}  # fmt: skip
    headers = {"set-cookie": f"cart_currency={cookie}; path=/; SameSite=Lax"} if cookie else {}
    return httpx.Response(200, json=body, headers=headers)


def test_shopify_prices_take_the_currency_their_page_states(mock):
    # lacabra.com, 2026-11-04: products.json gave "159.00" and set cart_currency=DKK. Page 2 doesn't set it again,
    # but its request carries the cookie page 1 set: the same currency, stated by the store. No extra request.
    p1 = mock.get("https://x.com/products.json", params={"page": "1"}).mock(
        return_value=_page(range(250), "DKK")
    )
    p2 = mock.get("https://x.com/products.json", params={"page": "2"}).mock(
        return_value=_page(range(250, 260))
    )
    mock.get("https://x.com/products.json", params={"page": "3"}).mock(return_value=_page([]))
    with PoliteClient(min_interval=0) as c:
        items = shopify.fetch(c, _r("x.com"))
    assert len(items) == 260 and {v.currency for i in items for v in i.variants} == {"DKK"}
    assert "cart_currency=DKK" in p2.calls[0].request.headers.get("cookie", "")
    assert p1.call_count == 1 and len(mock.calls) == 4  # robots.txt and three pages: nothing else asked


def test_shopify_without_the_cookie_states_no_currency(mock):
    mock.get("https://x.com/products.json", params={"page": "1"}).mock(return_value=_page([1]))
    mock.get("https://x.com/products.json", params={"page": "2"}).mock(return_value=_page([]))
    with PoliteClient(min_interval=0) as c:
        assert [v.currency for i in shopify.fetch(c, _r()) for v in i.variants] == [None]


def test_shopify_page_currency_reads_the_response_then_the_request():
    req = httpx.Request("GET", "https://x.com/products.json", headers={"cookie": "a=1; cart_currency=CAD"})
    assert shopify.page_currency(httpx.Response(200, request=req)) == "CAD"
    set_usd = httpx.Response(200, request=req, headers={"set-cookie": "cart_currency=USD; path=/"})
    assert shopify.page_currency(set_usd) == "USD"  # what this response states wins
    assert shopify.page_currency(httpx.Response(200)) is None
    assert (
        shopify.parse_product({"id": 1, "variants": [{"id": 2, "price": "1"}]}, _r(), "GBP")
        .variants[0]
        .currency
        == "GBP"
    )


# --- Squarespace: priceMoney.currency ------------------------------------------------------------


def test_squarespace_records_price_money_currency(roasters):
    items = [
        squarespace.parse_item(i, roasters["vividcoffee.com"])
        for i in load("squarespace_vividcoffee.json")["items"]
    ]
    assert {v.currency for i in items for v in i.variants} == {"USD"}
    item = {"id": "1", "title": "Kenya", "variants": [{"id": "a", "priceMoney": {"value": "18.00"}},
                                                      {"id": "b", "priceMoney": {"currency": "EUR", "value": "18.00"}}]}  # fmt: skip
    assert [v.currency for v in squarespace.parse_item(item, _r(platform="squarespace")).variants] == [
        None,
        "EUR",
    ]


# --- Square Online: the store page's storeInfo.currency --------------------------------------------

_SQUARE_PAGE = (
    '<script>window.__BOOTSTRAP_STATE__={"user":{"id":"131383667"},"site":{"site_id":"582503203142049854"},'
    '"storeInfo":{"merchant_id":"G7","payment_method_support":{"visa":true},"currency":"USD","locale":"en_US"}}'
    "</script>"
)


def test_square_online_reads_the_store_pages_currency():
    assert square_online.store_currency(_SQUARE_PAGE) == "USD"
    assert square_online.store_currency('var com_userID = "1"; var com_currentSite = "1234567";') is None


def test_square_online_fetch_records_it_on_every_product(mock):
    mock.get("https://x.com/").mock(return_value=httpx.Response(200, text=_SQUARE_PAGE))
    api = square_online.API.format(user="131383667", site="582503203142049854")
    product_ = {
        "id": "1",
        "name": "Kenya 12 oz",
        "site_link": "product/kenya/1",
        "price": {"regular_low": 18, "regular_high": 18},
    }
    mock.get(api).mock(
        return_value=httpx.Response(
            200, json={"data": [product_], "meta": {"pagination": {"total_pages": 1}}}
        )
    )
    with PoliteClient(min_interval=0) as c:
        items = square_online.fetch(c, _r(platform="square_online"))
    assert [(v.price_usd, v.currency) for i in items for v in i.variants] == [(18.0, "USD")]


# --- generic: schema.org priceCurrency ---------------------------------------------------------------


def test_generic_reads_price_currency_from_the_offer_its_specification_or_its_aggregate():
    p = {
        "name": "Kenya",
        "offers": {
            "@type": "AggregateOffer", "priceCurrency": "SGD",
            "offers": [
                {"sku": "a", "price": "20", "priceCurrency": "USD"},
                {"sku": "b", "price": "20", "priceSpecification": {"price": "20", "priceCurrency": "EUR"}},
                {"sku": "c", "price": "20"},
            ],
        },
    }  # fmt: skip
    item = generic.parse_jsonld(p, "https://x.com/product/kenya", _r(platform="generic"))
    assert [v.currency for v in item.variants] == ["USD", "EUR", "SGD"]
    bare = generic.parse_jsonld(
        {"name": "K", "offers": {"price": "9"}}, "https://x.com/p/k", _r(platform="generic")
    )
    assert [v.currency for v in bare.variants] == [None]


def test_generic_wix_fixture_states_usd():
    html = (FIX / "wix_product_jsonld.html").read_text()
    item = generic.parse_jsonld(
        generic.jsonld_products(html)[0], "https://x.com/product-page/x", _r(platform="generic")
    )
    assert {v.currency for v in item.variants} == {"USD"}


# --- the registry ------------------------------------------------------------------------------------

# Hand-checked 2026-11-04 (PR body): the store's own currency or country, or its country-code domain.
NOT_US = {
    "aromacoffee.co.za": "ZA",
    "blacksheepcoffee.co.uk": "GB",
    "lacabra.dk": "DK",
    "macacotostadores.com": "MX",
    "plantationcoffee.com.au": "AU",
    "sierracoffee.co.nz": "NZ",
    "thecoffeeroaster.sg": "SG",
}


def test_the_seed_corrects_the_roasters_outside_the_us():
    with (Path(__file__).parent.parent / "config" / "roasters.csv").open() as f:
        rows = {r["roaster_id"]: r for r in csv.DictReader(f)}
    assert {rid: r["country"] for rid, r in rows.items() if r["country"] != "US"} == NOT_US
    assert all(rows[rid]["state"] == "" for rid in NOT_US)  # no US state for a store outside the US
    assert rows["aromacoffee.co.za"]["city"] == "" and rows["lacabra.dk"]["city"] == "Nordhavn"
    assert all(r["country"] == "US" for r in rows.values() if r["roaster_id"] not in NOT_US)


# --- dbt: USD prices and US roasters only in the public tables ---------------------------------------

dbt = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")


def _config_with(tmp_path, *extra: dict) -> Path:
    """The fixture registry plus extra seed rows (e.g. a roaster outside the US)."""
    out = tmp_path / "config"
    out.mkdir()
    with (FIX / "roasters.csv").open() as f:
        rows = list(csv.DictReader(f))
    with (out / "roasters.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        for e in extra:
            w.writerow({k: "" for k in rows[0]} | {"sources": "overture", "platform": "woocommerce"} | e)
    return out


def _priced(pid: str, price: float, currency: str | None) -> dict:
    p = product(pid, price)
    p["variants"][0]["currency"] = currency
    return p


@dbt
def test_public_tables_publish_usd_prices_and_us_roasters_only(lake, tmp_path, monkeypatch):
    from .test_dbt import build

    us, za = "onyxcoffeelab.com", "aromacoffee.co.za"
    config = _config_with(tmp_path, {"roaster_id": za, "name": "Aroma", "final_host": za, "country": "ZA"})
    for rid in (us, za):
        probe(lake, "2026-10-18", rid, platform="woocommerce")
    # Week 1 was written before raw 1.6.0: no currency in the file at all.
    old = lake_mod.SCHEMAS["snapshots"]
    old_variant = lake_mod.pa.struct([f for f in lake_mod.VARIANT if f.name != "currency"])
    i = old.get_field_index("variants")
    monkeypatch.setitem(
        lake_mod.SCHEMAS, "snapshots", old.set(i, old.field(i).with_type(lake_mod.pa.list_(old_variant)))
    )
    run(
        lake,
        "2026-10-19",
        us,
        [product("A", 20.0), product("B", 18.0), product("C", 24.0)],
        adapter="woocommerce",
    )
    run(lake, "2026-10-19", za, [product("Z", 135.0)], adapter="woocommerce")
    monkeypatch.setitem(lake_mod.SCHEMAS, "snapshots", old)
    # Week 2: stated currencies. C's store now states CAD; D states usd in lower case.
    week2 = [
        _priced("A", 21.0, "USD"),
        product("B", 18.0),
        _priced("C", 25.0, "CAD"),
        _priced("D", 30.0, " usd "),
    ]
    run(lake, "2026-10-26", us, week2, adapter="woocommerce")
    run(lake, "2026-10-26", za, [_priced("Z", 169.0, "ZAR")], adapter="woocommerce")
    con = build(lake, tmp_path, config_dir=str(config))

    def q(sql):
        return con.execute(sql).fetchall()

    prices = dict(
        q("select product_id, min_price_usd from dim_coffee where roaster_id = 'onyxcoffeelab.com'")
    )
    # USD as stated (any case); no currency from a US roaster, as before; a stated CAD price is never dollars.
    assert prices == {"A": 21.0, "B": 18.0, "C": None, "D": 30.0}
    # The ZA store: in the internal marts, unpriced; never in the public tables.
    assert q(
        "select min_price_usd, price_per_100g, roaster_country from dim_coffee where roaster_id = 'aromacoffee.co.za'"
    ) == [(None, None, "ZA")]
    assert q("select country from dim_roaster where roaster_id = 'aromacoffee.co.za'") == [("ZA",)]
    assert q("select count(*) from public_roaster where roaster_id = 'aromacoffee.co.za'") == [(0,)]
    assert q("select count(*) from public_coffee where roaster_id = 'aromacoffee.co.za'") == [(0,)]
    assert q("select count(*) from public_price_event where roaster_id = 'aromacoffee.co.za'") == [(0,)]
    # Price events: A's change is one; C's first stated currency is not a price change from 24 to nothing.
    events = {(r[0], r[1]): r[2:] for r in q(
        "select coffee_id, run_date::varchar, event, price_usd, previous_price_usd from public_price_event")}  # fmt: skip
    assert events[(f"{us}:A", "2026-10-26")] == ("price", 21.0, 20.0)
    assert (f"{us}:C", "2026-10-26") not in events
    assert events[(f"{us}:C", "2026-10-19")] == ("new", 24.0, None)
    con.close()
    # The raw lake keeps what the store stated.
    raw = duckdb.sql(f"""select v.price_usd, v.currency from read_parquet('{lake.root}/snapshots/*/*.parquet',
        union_by_name = true), unnest(variants) t(v) where roaster_id = '{za}' order by run_date""").fetchall()  # fmt: skip
    assert raw == [(135.0, None), (169.0, "ZAR")]


@dbt
def test_old_rows_of_a_us_roaster_publish_as_before(lake, tmp_path):
    """A lake with no currency anywhere (every row before raw 1.6.0) builds the same public prices as before."""
    from .test_dbt import build

    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    run(lake, "2026-10-19", rid, [product("A", 20.0)])
    run(lake, "2026-10-26", rid, [product("A", 22.0)])
    con = build(lake, tmp_path)
    assert con.execute("select min_price_usd from public_coffee").fetchall() == [(22.0,)]
    assert con.execute(
        "select event, price_usd, previous_price_usd from public_price_event where event = 'price'"
    ).fetchall() == [("price", 22.0, 20.0)]
    assert date(2026, 10, 26) in {
        r[0] for r in con.execute("select run_date from public_price_event").fetchall()
    }
