"""The 2026-11-24 audit of the 71 generic stores that failed on 2026-11-16: each store's robots.txt and home page, read
once each under robots.txt, through the probe's own detector (discover.HTML_MARKERS). The PR body lists the stores.

Only unrivaledcoffee.com's seed row was wrong: the probe on 2026-10-26 found no platform marker (`other`), and the
store now serves Shopify's robots.txt and theme. Its seed platform is the probe's answer for that page, `shopify?`,
which keeps it on the generic connector on main (models.adapter_of) until a change for `shopify?` stores moves them.

34 domains no longer serve a roaster (parked, no DNS record, or an unrelated site): each is a not_a_roaster row of
dbt's seed shared_hosts, with its reason, as galvestoncoffeeroasters.com is. Stores whose failure could be temporary
stay in the registry."""

from __future__ import annotations

import httpx
import respx

from coffeedata import discover, registry
from coffeedata.http import PoliteClient
from coffeedata.models import Roaster, adapter_of

# Stores whose robots.txt ("# Shopify storefront") and home page (cdn.shopify.com, Shopify.theme) are Shopify's.
SHOPIFY = [
    "21queenst.com",
    "blackbirdcoffeeroasters.com",
    "blackdogcoffeeroasters.com",
    "blessedbeancoffee.com",
    "curleycoffee.com",
    "goldcoffee.com",
    "grumpymancoffee.com",
    "ikoncoffee.com",
    "kinetic-koffee.com",
    "manzanitaroasting.com",
    "mercerscafe.com",
    "metriccoffee.com",
    "parabolacoffee.com",
    "presscoffee.com",
    "proudmarycoffee.com",
    "realdope.coffee",
    "roseparkroasters.com",
    "sbcoffee.com",
    "strangelywarmed.com",
    "unrivaledcoffee.com",
    "vicecitybean.com",
]
# WordPress with WooCommerce's markup on the home page.
WOOCOMMERCE = ("frothymonkey.com", "northboundcoffee.com")


def _seed() -> dict[str, dict]:
    return {r["roaster_id"]: r for r in registry.load_seed()}


def test_unrivaled_coffee_has_the_probes_answer_for_its_shopify_storefront():
    row = _seed()["unrivaledcoffee.com"]
    assert (row["final_host"], row["platform"], row["last_probed"]) == (
        "unrivaledcoffee.com",
        "shopify?",
        "2026-11-24",
    )
    roaster = Roaster(
        roaster_id="unrivaledcoffee.com", name=row["name"], host=row["final_host"], platform="shopify?"
    )
    assert roaster.adapter == "generic"  # unchanged on main: no product's output changes


def test_every_shopify_and_woocommerce_store_of_the_audit_has_its_marker_platform_in_the_seed():
    seed = _seed()
    assert {h: seed[h]["platform"] for h in SHOPIFY} == dict.fromkeys(SHOPIFY, "shopify?")
    assert {h: seed[h]["platform"] for h in WOOCOMMERCE} == dict.fromkeys(WOOCOMMERCE, "woocommerce?")
    assert {adapter_of(seed[h]["platform"]) for h in (*SHOPIFY, *WOOCOMMERCE)} == {"generic"}


def test_the_correction_reaches_the_registry_state(lake):
    """registry.current reads the seed's platform when the lake has no probe for the roaster."""
    states = {s.roaster.roaster_id: s.roaster for s in registry.current(lake)}
    assert (states["unrivaledcoffee.com"].host, states["unrivaledcoffee.com"].platform) == (
        "unrivaledcoffee.com",
        "shopify?",
    )


def test_the_probe_reads_a_shopify_theme_whose_catalog_api_did_not_answer_as_shopify_maybe():
    """The reproduced case: Shopify's 2026 robots.txt and a theme page, with /products.json not answering a catalog
    (a 404 here), is `shopify?` by the probe's marker rule, not `other`."""
    robots = "# Shopify storefront. Public product, collection, page, blog, policy, cart, and localized HTML is crawlable.\n"
    robots += "User-agent: *\nDisallow: /admin\nDisallow: /cart\nSitemap: https://a.com/sitemap.xml\n"
    home = (
        '<html><head><link rel="preconnect" href="https://cdn.shopify.com"><script>Shopify.theme = {"id": 8};'
    )
    home += "</script></head><body>Unrivaled</body></html>"
    with respx.mock as m:
        m.get("https://a.com/robots.txt").mock(return_value=httpx.Response(200, text=robots))
        m.get("https://a.com/").mock(return_value=httpx.Response(200, text=home))
        m.get(url__regex=r"https://a\.com/(products\.json|wp-json/.*)").mock(return_value=httpx.Response(404))
        with PoliteClient(min_interval=0, retries=1) as c:
            out = discover.probe_one(c, "a.com")
    assert (out["status"], out["platform"]) == ("ok", "shopify?")


# Retired: not_a_roaster rows (registry.NOT_A_ROASTER_SITE), dropped by registry.current and dbt's stg_registry.
RETIRED = [
    "asadocoffee.com",
    "aspirecoffeeroasters.com",
    "batdorf.com",
    "baysidebeans.com",
    "blueoakcoffee.com",
    "caminocoffeeroasters.com",
    "columbiarivercoffeeroaster.com",
    "ethiopiancoffeedistributor.com",
    "fusionroasters.com",
    "hazelnutcoffeecompany.com",
    "justbeanscoffee.com",
    "localroasting.com",
    "lordwindsor.com",
    "miamicoffeeroasting.com",
    "obxroco.com",
    "papertigercoffee.com",
    "pascalcoffee.com",
    "rebelroasterscoffee.com",
    "sogcoffee.com",
    "storymakerscoffee.com",
    "talegacoffeeco.com",
    "thecoffeepeddlers.com",
    "truebeans.com",
    "tugboatjoecoffee.com",
    "ggroastery.com",
    "zorrocoffee.com",
    "laslilascoffee.com",
    "theranch.net",
    "kfcoffee.com",
    "abracoffee.com",
    "queenanneroasters.com",
    "xeniaroastery.com",
    "roastedinthevillage.com",
    "thirdwavecoffeechicago.com",
]
# Kept: a failure that could be temporary (TLS, a 404, a reset, a Cloudflare block, a DNS timeout), a redirect to a
# site not clearly another business (heart2heartcoffee.com), and a lead only (sharedroasting.com's shop on Shopify).
KEPT = [
    "picachocoffee.com",
    "jackiesjava.com",
    "hickorygrovecoffee.com",
    "greenmountaincoffee.com",
    "nobletreecoffee.com",
    "spottedcowcoffeecompany.com",
    "heart2heartcoffee.com",
    "sharedroasting.com",
]


def test_the_retired_domains_are_not_roasters_with_a_reason():
    rows = {r["host"]: r for r in registry._shared_hosts()}
    assert len(RETIRED) == len(set(RETIRED)) == 34
    for host in RETIRED:
        assert registry.not_a_roaster_site(host), host
        assert rows[host]["kind"] == "not_a_roaster" and "2026-11-24 audit" in rows[host]["note"], host
        assert not registry.is_storefront_host(host), host  # discovery never adds it back
    assert not any(registry.not_a_roaster_site(h) for h in KEPT)


def test_the_registry_drops_the_retired_domains_and_keeps_the_rest(lake):
    seed = _seed()
    assert set(RETIRED) <= set(seed)  # the seed rows stay, as galvestoncoffeeroasters.com's does
    ids = {s.roaster.roaster_id for s in registry.current(lake)}
    assert not ids & set(RETIRED)
    assert set(KEPT) | set(SHOPIFY) | set(WOOCOMMERCE) <= ids
