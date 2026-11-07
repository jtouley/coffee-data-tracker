"""The discovery screen (screen.py): cafés and coffee shops are candidates, and only a screen that finds a roaster
selling its own coffee online admits one. Cases from the coverage investigation's Philadelphia pilot."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from coffeedata import discover, registry, screen
from coffeedata.models import CatalogItem, Variant

KNOWN = screen.KnownRoasters(
    [
        "Counter Culture Coffee",
        "Rival Bros Coffee",
        "Passenger Coffee",
        "Vibrant Coffee Roasters",
        "Single Origin",
    ]
)


def _shopify(host: str, home: str, products: list[dict]) -> respx.MockRouter:
    m = respx.mock(assert_all_called=False)
    m.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
    m.get(f"https://{host}/").mock(
        return_value=httpx.Response(
            200, html=f"<html><script src='//cdn.shopify.com/x.js'></script>{home}</html>"
        )
    )

    def page(request: httpx.Request) -> httpx.Response:
        first = request.url.params.get("page", "1") == "1" or request.url.params.get("limit") == "1"
        return httpx.Response(200, json={"products": products if first else []})

    m.get(f"https://{host}/products.json").mock(side_effect=page)
    m.route().mock(return_value=httpx.Response(404))
    return m


def _product(i: int, title: str, vendor: str, ptype: str = "Coffee", body: str = "") -> dict:
    return {"id": i, "handle": f"p{i}", "title": title, "vendor": vendor, "product_type": ptype, "tags": [],
            "body_html": body, "variants": [{"id": i * 10, "title": "12 oz", "price": "18.00", "available": True}]}  # fmt: skip


def _screen(domain: str, name: str, **kw) -> dict:
    with screen.ScreenClient(min_interval=0, retries=1) as c:
        return screen.screen_one(c, domain, name, KNOWN, **kw)


# --- the pilot's cases -----------------------------------------------------------------------------------------------


def test_rival_bros_a_roaster_tagged_coffee_shop_is_admitted():
    products = [
        _product(1, "Whittaker Espresso Blend", "Rival Bros Coffee"),
        _product(2, "Revolver - Medium Roast", "Rival Bros Coffee"),
        _product(3, "Ethiopia Guji Natural", "Rival Bros Coffee"),
        _product(4, "Rival Bros Mug", "Rival Bros Coffee", ptype="Merch"),
    ]
    with _shopify("rivalbros.com", "<p>We roast our coffee in Philadelphia.</p>", products):
        out = _screen("rivalbros.com", "Rival Bros Coffee")
    assert (out["status"], out["platform"], out["coffees"], out["admitted"]) == ("ok", "shopify", 3, True)
    assert out["reason"].startswith("roaster:")


def test_cafe_tolia_a_cafe_with_no_beans_is_rejected():
    m = respx.mock(assert_all_called=False)
    m.get("https://cafetolia.com/robots.txt").mock(return_value=httpx.Response(404))
    m.get("https://cafetolia.com/").mock(
        return_value=httpx.Response(
            200, html="<html><script src='https://static1.squarespace.com/x.js'></script>"
            "<p>Cafe Tolia proudly serves Rival Bros Coffee.</p></html>"
        )
    )  # fmt: skip
    m.get("https://cafetolia.com/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text="<urlset>" + "".join(f"<url><loc>https://cafetolia.com/{p}</loc></url>"
                                           for p in ("about-1", "contact", "menu", "home-1")) + "</urlset>"
        )
    )  # fmt: skip
    m.route().mock(return_value=httpx.Response(404))
    with m:
        out = _screen("cafetolia.com", "Cafe Tolia")
    assert (out["platform"], out["products"], out["admitted"], out["reason"]) == (
        "squarespace?",
        0,
        False,
        "no_catalog",
    )
    assert out["serves_phrase"] == "proudly serves Rival Bros Coffee"


def test_a_shop_selling_other_roasters_bags_is_rejected():
    products = [
        _product(1, "Counter Culture Hologram", "Counter Culture Coffee"),
        _product(2, "Big Trouble", "Counter Culture Coffee"),
        _product(3, "Chimney Rock Blend", "Passenger Coffee"),
        _product(4, "Corner Shop House Blend", "Corner Shop"),
    ]
    with _shopify("cornershop.example", "<p>Coffee, pastries and the best beans in town.</p>", products):
        out = _screen("cornershop.example", "Corner Shop")
    assert (out["coffees"], out["other_roaster"], out["admitted"]) == (4, 3, False)
    assert out["reason"].startswith("reseller:")


def test_a_cafe_that_says_it_serves_another_roaster_and_never_that_it_roasts_is_rejected():
    products = [
        _product(1, "House Blend 12oz", "Two Persons Coffee"),
        _product(2, "Espresso Blend", "Two Persons"),
    ]
    home = "<p>We proudly serve Passenger Coffee from Lancaster.</p>"
    with _shopify("twopersonscoffee.example", home, products):
        out = _screen("twopersonscoffee.example", "Two Persons Coffee")
    assert (out["admitted"], out["serves_phrase"]) == (False, "proudly serve Passenger Coffee")
    assert out["reason"].startswith("reseller: says")


def test_bean2bean_a_roaster_tagged_cafe_selling_own_brand_coffee_and_tea_is_admitted():
    products = [
        _product(1, "HONDURAS MONTECILLOS", "Bean2Bean Coffee Co"),
        _product(2, "PAWS Roast", "Bean2Bean Coffee Co."),
        _product(3, "Earl Grey Tea", "Bean2Bean Coffee Co.", ptype="Tea"),
    ]
    with _shopify("bean2bean.com", "<p>Coffee and tea.</p>", products):
        out = _screen("bean2bean.com", "Bean2Bean Coffee Co.")
    assert (out["coffees"], out["own_vendor"], out["admitted"]) == (2, 2, True)
    assert out["reason"].startswith("own_brand:")


def test_a_cafe_store_with_only_merch_is_rejected():
    products = [
        _product(1, "Logo Mug", "Cafe Square One", ptype="Merch"),
        _product(2, "Gift Card", "Cafe Square One"),
    ]
    with _shopify("cafesquareone.example", "<p>Serving Vibrant Coffee Roasters.</p>", products):
        out = _screen("cafesquareone.example", "Cafe Square One")
    assert (out["products"], out["coffees"], out["admitted"]) == (2, 0, False)
    assert out["reason"].startswith("no_coffee")


def test_an_unreachable_candidate_is_rejected_as_no_answer():
    m = respx.mock(assert_all_called=False)
    m.route().mock(side_effect=httpx.ConnectError("down"))
    with m:
        out = _screen("gone.example", "Gone")
    assert (out["status"], out["admitted"], out["reason"]) == ("unreachable", False, "unreachable")


def test_every_rule_rejects_too_few_coffees_and_the_strict_ones_need_evidence():
    no_evidence = screen.Signals(
        products=5, coffees=3, named=3, own_vendor=0, other_roaster=0, roast_phrase="", serves_phrase=""
    )
    assert screen.rule_coffee(no_evidence, 1)[0] and screen.rule_not_reseller(no_evidence, 1)[0]
    assert not screen.rule_own_brand(no_evidence, 1)[0] and not screen.rule_roast_words(no_evidence, 1)[0]
    assert not any(r(no_evidence, 5)[0] for r in screen.RULES.values())


# --- names -----------------------------------------------------------------------------------------------------------


def test_title_matching_ignores_blend_names_and_matches_multi_word_roasters():
    assert KNOWN.in_title("Champion Blend Autumn", "rival bros") is None
    assert KNOWN.in_title("Single Origin Colombia", "rival bros") is None  # a coffee word, not the roaster
    assert KNOWN.in_title("Counter Culture Hologram", "corner") == "counter culture"
    assert KNOWN.in_title("Rival Bros Revolver", "rival bros") is None  # its own name
    assert screen.is_own("Rival Bros Coffee", "Rival Bros", "rivalbros.com")
    assert screen.is_own("RIVALBROS", "Rival Brothers", "rivalbros.com")
    assert not screen.is_own("Counter Culture Coffee", "Corner Shop", "cornershop.example")
    # never a substring: another roaster's bags at a café whose name is part of the roaster's
    assert not screen.is_own("Counter Culture Coffee", "The Counter", "thecounter.example")
    assert not screen.is_own("Peet's Coffee", "Peet Street Cafe", "peetstreetcafe.example")


@pytest.mark.parametrize(
    ("home", "serves"),
    [
        ("We serve Stumptown coffee and fresh pastries.", "We serve Stumptown"),
        ("Proudly serving Rival Bros every morning.", "Proudly serving Rival Bros"),
        ("We serve a full breakfast menu.", ""),
        ("Proudly serving Portland since 2005.", ""),
        ("Proudly serving our community.", ""),
    ],
)
def test_serves_wording_needs_a_named_roaster_or_coffee_words(home, serves):
    assert screen.signals([], home, "Corner", "corner.example", KNOWN).serves_phrase == serves


def test_roastery_wording_copied_into_a_resellers_product_descriptions_is_not_the_stores():
    products = [
        _product(1, "Chimney Rock Blend", "Passenger Coffee", body="Small batch roasted in Lancaster."),
        _product(2, "Corner House Blend", "Corner Shop", body="We roast every batch by hand."),
    ]
    with _shopify("cornershop.example", "<p>We proudly serve Passenger Coffee.</p>", products):
        out = _screen("cornershop.example", "Corner Shop")
    assert (out["roast_phrase"], out["admitted"]) == ("", False)


# --- discovery: candidates, never the registry -----------------------------------------------------------------------

# OVERTURE_SQL's columns: website, name, city, state, confidence, tagged, roast_in_name, is_roaster, tag, open
PLACES = [
    ("https://rivalbros.com/", "Rival Bros Coffee", "Philadelphia", "PA", 0.9, False, False, False, "coffee_shop", True),
    ("https://rivalbros.com/", "Rival Bros Coffee", "Philadelphia", "PA", 0.8, False, False, False, "coffee_shop", True),
    ("http://www.cafetolia.com/", "Cafe Tolia", "Philadelphia", "PA", 0.99, False, False, False, "cafe", True),
    ("https://instagram.com/harazcoffee", "Haraz Coffee House", "Philadelphia", "PA", 0.9, False, False, False, "coffee_shop", True),
    ("https://linktr.ee/haraz", "Haraz Coffee House", "Philadelphia", "PA", 0.9, False, False, False, "cafe", True),
    ("https://passeroscoffee.com/", "Passero's Coffee Roasters", "Philadelphia", "PA", 0.9, False, True, True, "coffee_shop", True),
    ("https://bean2bean.com/", "Bean2Bean Coffee Co.", "Philadelphia", "PA", 0.7, False, False, False, "cafe", True),
    ("https://closed.example/", "Closed Cafe", "Philadelphia", "PA", 0.9, False, False, False, "cafe", False),
    (None, "Ellis Coffee", "Philadelphia", "PA", 0.9, False, False, False, "coffee_shop", True),
]  # fmt: skip


def test_cafes_and_coffee_shops_become_candidates_not_roasters():
    roasters, candidates = discover.places_to_rows(PLACES, "overture-2026-10-21.0")
    assert [r["roaster_id"] for r in roasters] == ["passeroscoffee.com"]
    by_id = {c["roaster_id"]: c for c in candidates}
    # Haraz (social links only), Ellis (no website) and the closed cafe are dropped; Rival Bros is one row (a chain
    # counts once)
    assert sorted(by_id) == ["bean2bean.com", "cafetolia.com", "rivalbros.com"]
    assert (by_id["rivalbros.com"]["locations"], by_id["rivalbros.com"]["tag"]) == (2, "coffee_shop")
    assert by_id["cafetolia.com"]["tag"] == "cafe"


def test_a_domain_that_is_a_roaster_by_the_rule_is_never_also_a_candidate():
    places = [
        ("https://x.example/", "X Roasters", "A", "PA", 0.9, False, True, True, "coffee_shop", True),
        ("https://x.example/", "X Cafe", "A", "PA", 0.9, False, False, False, "cafe", True),
    ]
    roasters, candidates = discover.places_to_rows(places, "s")
    assert [r["roaster_id"] for r in roasters] == ["x.example"] and candidates == []
    assert roasters[0]["locations"] == 1  # the café place isn't counted as a roaster location


# --- which candidates, and the run -----------------------------------------------------------------------------------

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _cands(lake, ids):
    lake.write(
        "candidates",
        "candidates/source=overture/t.parquet",
        [{"added_at": NOW, "source": "overture-r", "roaster_id": d, "name": d, "tag": "cafe"} for d in ids],
    )


def _screened(lake, rows):
    lake.write_partition(
        "screens",
        "2026-01-01",
        "b0",
        [{"run_date": date(2026, 1, 1), "batch_id": "b0", "roaster_id": d, "screened_at": at, "status": st,
          "admitted": adm, "reason": why} for d, at, st, adm, why in rows],
    )  # fmt: skip


def test_select_orders_new_first_skips_known_and_admitted_and_waits_out_rejections(lake):
    _cands(lake, ["new1.example", "new2.example", "cut.example", "old_no.example", "recent_no.example", "down.example",
                  "admitted.example", "known.example", "robots.example", "dead.example", "instagram.com"])  # fmt: skip
    _screened(lake, [
        ("old_no.example", NOW - timedelta(days=200), "ok", False, "no_coffee: 0 of 3 products"),
        ("recent_no.example", NOW - timedelta(days=100), "ok", False, "no_catalog"),
        ("down.example", NOW - timedelta(days=30), "http_503", False, "http_503"),
        ("dead.example", NOW - timedelta(days=30), "unreachable", False, "unreachable"),
        ("admitted.example", NOW - timedelta(days=300), "ok", True, "roaster"),
        ("robots.example", NOW - timedelta(days=1), "robots_unavailable", False, "robots_unavailable"),
        ("cut.example", NOW - timedelta(days=1), "deadline", False, "deadline: the run's deadline"),
    ])  # fmt: skip
    got = [
        c["roaster_id"]
        for c in screen.select(lake, "2026-11-07", known={"known.example", "admitted.example"})
    ]
    # cut.example's screen was cut off by the run's deadline: no screen, so new; a dead domain waits 180 days, a
    # robots.txt that didn't answer 28
    new = sorted(["new1.example", "new2.example", "cut.example"], key=screen.order_key)
    assert got == [*new, "old_no.example", "down.example"]  # re-screens oldest first
    assert [c["roaster_id"] for c in screen.select(lake, "2026-11-07", known=set(), cap=2)] == new[:2]
    assert [c["roaster_id"] for c in screen.select(lake, "2026-11-07", set(), only="recent_no.example")] == [
        "recent_no.example"
    ]


def test_an_admitted_candidate_joins_the_registry_with_its_probe(lake, monkeypatch):
    _cands(lake, ["roaster.example", "cafe.example"])
    fake = {
        "roaster.example": {"status": "ok", "http_status": 200, "final_host": "roaster.example", "platform": "shopify",
                          "admitted": True, "reason": "roaster"},
        "cafe.example": {"status": "ok", "http_status": 200, "final_host": "www.cafe.example",
                          "platform": "squarespace?", "admitted": False, "reason": "no_catalog"},
    }  # fmt: skip
    monkeypatch.setattr(
        screen, "screen_one", lambda c, d, *a, **k: {k2: None for k2 in screen.SCREEN_FIELDS} | fake[d]
    )
    todo = screen.select(lake, "2026-11-07", known=set())
    # without --admit: screens recorded, nobody admitted, and the one the rule would admit is screened again later
    dry = screen.run(lake, todo, ["Rival Bros Coffee"], "2026-11-01", workers=2)
    assert (dry["screened"], dry["admitted"], dry["admit"]) == (2, 0, False)
    assert "roaster.example" not in {s.roaster.roaster_id for s in registry.current(lake)}
    todo = screen.select(lake, "2026-11-07", known=set())
    assert [c["roaster_id"] for c in todo] == ["roaster.example"]
    out = screen.run(lake, todo, ["Rival Bros Coffee"], "2026-11-07", workers=2, admit=True)
    assert (out["admitted"], out["reasons"]) == (1, {"roaster": 1})
    states = {s.roaster.roaster_id: s for s in registry.current(lake)}
    assert "cafe.example" not in states
    rival = states["roaster.example"]
    assert (rival.roaster.platform, rival.roaster.host, rival.status) == (
        "shopify",
        "roaster.example",
        "pending",
    )
    # screened and admitted: a registry roaster from then on, never selected again
    assert screen.select(lake, "2026-11-07", known=set(registry.known(lake))) == []


# --- the budget ------------------------------------------------------------------------------------------------------


def test_the_page_budget_stops_a_catalog_read_and_what_was_read_counts(monkeypatch):
    monkeypatch.setattr(screen, "PAGE_BUDGET", 2)
    m = respx.mock(assert_all_called=False)
    m.get("https://big.example/robots.txt").mock(return_value=httpx.Response(404))
    m.get("https://big.example/").mock(return_value=httpx.Response(200, html="<p>We roast every day.</p>"))
    m.get("https://big.example/sitemap.xml").mock(
        return_value=httpx.Response(200, text="<urlset>" + "".join(
            f"<url><loc>https://big.example/products/coffee-{i}</loc></url>" for i in range(10)) + "</urlset>")
    )  # fmt: skip
    ld = '<script type="application/ld+json">{"@type": "Product", "name": "Kenya AA Whole Bean", "sku": "%s"}</script>'
    pages = m.get(url__regex=r"https://big\.example/products/coffee-\d").mock(
        side_effect=lambda r: httpx.Response(200, html=ld % r.url.path)
    )
    m.route().mock(return_value=httpx.Response(404))
    with m:
        out = _screen("big.example", "Big")
        assert (
            pages.call_count == 1
        )  # the sitemap, then one product page: the budget of 2 (respx resets on exit)
    assert (out["platform"], out["partial"], out["coffees"], out["admitted"]) == ("other", True, 1, True)


def test_screen_items_are_catalog_items():
    it = CatalogItem(roaster_id="", product_id="1", handle="", url="", title="Kenya AA", platform="",
                     variants=[Variant(variant_id="1", grams=340)])  # fmt: skip
    assert screen.signals([it], "", "X", "x.example", KNOWN).coffees == 1


@pytest.mark.parametrize(
    ("last", "due"),
    [
        (None, True),
        (
            {"admitted": True, "status": "ok", "reason": "roaster", "days": 1},
            True,
        ),  # select drops registry ones
        ({"admitted": False, "status": "http_429", "reason": "http_429", "days": 28}, True),
        ({"admitted": False, "status": "ok", "reason": "no_catalog", "days": 179}, False),
        ({"admitted": False, "status": "ok", "reason": "no_catalog", "days": 180}, True),
        ({"admitted": False, "status": "blocked", "reason": "blocked", "days": 100}, False),
        ({"admitted": False, "status": "budget_spent", "reason": "budget_spent: x", "days": 100}, False),
        ({"admitted": False, "status": "http_503", "reason": "http_503", "days": 28}, True),
        ({"admitted": False, "status": "unreachable", "reason": "unreachable", "days": 100}, False),
        (
            {"admitted": False, "status": "robots_unavailable", "reason": "robots_unavailable", "days": 27},
            False,
        ),
        (
            {"admitted": False, "status": "robots_unavailable", "reason": "robots_unavailable", "days": 28},
            True,
        ),
        ({"admitted": False, "status": "ok", "reason": "catalog_error: HTTP 500", "days": 27}, False),
        ({"admitted": False, "status": "ok", "reason": "catalog_error: HTTP 500", "days": 28}, True),
    ],
)
def test_due(last, due):
    if last is not None:
        last = last | {"screened_at": NOW - timedelta(days=last.pop("days"))}
    assert screen.due(last, NOW.date()) is due


def test_a_gift_shops_jams_are_not_counted_as_named_coffee():
    # the train set's gelato-and-jam store: the rules call these coffee, but none is named as one
    items = [
        CatalogItem(roaster_id="", product_id=str(n), handle="", url="", title=t, platform="", vendor="Spoon")
        for n, t in enumerate(["Holiday Hits", "Sugar Plum Jam", "Boozy Butterscotch"])
    ]
    s = screen.signals(items, "", "Spoon", "spoon.example", KNOWN)
    assert s.named == 0 and not screen.rule_own_brand(s, 1)[0]


def test_a_busy_home_page_is_http_status_not_unreachable():
    m = respx.mock(assert_all_called=False)
    m.get(url__regex=r"https://(www\.)?busy\.example/robots\.txt").mock(return_value=httpx.Response(404))
    m.get(url__regex=r"https://(www\.)?busy\.example/$").mock(return_value=httpx.Response(503))
    with m, screen.ScreenClient(min_interval=0, retries=1) as c:
        assert (
            discover.probe_one(c, "busy.example")["status"] == "http_503"
        )  # re-screened after 28 days, not 180


def test_known_lists_seed_and_registry_roasters_without_reading_catalogs(lake, tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("roaster_id,name\nrivalbros.com,Rival Bros Coffee\n")
    lake.write("registry", "registry/source=manual/x.parquet",
               [{"added_at": NOW, "source": "manual", "roaster_id": "new.example", "name": "New"}])  # fmt: skip
    assert registry.known(lake, seed) == {"rivalbros.com": "Rival Bros Coffee", "new.example": "New"}
