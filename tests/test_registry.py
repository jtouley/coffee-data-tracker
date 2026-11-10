from pathlib import Path

import pytest

from coffeedata import registry

from .lakehelp import probe, product, run

SEED = Path(__file__).parent / "fixtures" / "roasters.csv"


def test_normalize_domain():
    assert registry.normalize_domain("https://WWW.Example.com/shop?x=1") == "example.com"
    assert registry.normalize_domain("locations.dutchbros.com") == "dutchbros.com"


@pytest.mark.parametrize(
    "host,ok",
    [
        ("joes.business.site", False),
        ("shop.square.site", False),
        ("x.order.online", False),
        ("instagram.com", False),
        ("linktr.ee", False),
        ("onyxcoffeelab.com", True),
        ("squareonecoffee.com", True),
        ("nodot", False),
    ],
)
def test_storefront_hosts(host, ok):
    assert registry.is_storefront_host(host) is ok


def test_seed_file_is_valid():
    rows = registry.load_seed()
    assert len(rows) > 1000 and len({r["roaster_id"] for r in rows}) == len(rows)


def status(lake, rid):
    return {s.roaster.roaster_id: s for s in registry.current(lake, SEED)}[rid]


def test_state_from_history(lake):
    rid = "onyxcoffeelab.com"
    assert status(lake, rid).status == "pending"  # seed platform, never fetched
    run(lake, "2026-10-26", rid, [product("1")])
    assert status(lake, rid).status == "active"
    # A failed probe never erases a known platform (review finding 10).
    probe(lake, "2026-11-01", rid, status="unreachable")
    s = status(lake, rid)
    assert s.status == "active" and s.roaster.platform == "shopify"
    # Unreachable only after 3 failed *runs*; retries inside one run don't count extra.
    for d in ("2026-11-02", "2026-11-02", "2026-11-09"):
        run(lake, d, rid, None, hour=12 + len(d) % 3)
    assert status(lake, rid).status == "active"
    run(lake, "2026-11-16", rid, None)
    assert status(lake, rid).status == "unreachable"
    run(lake, "2026-11-23", rid, [product("1", coffee=False)])
    assert status(lake, rid).status == "no_coffee"


def test_opt_out_via_lake(lake):
    from datetime import UTC, datetime

    lake.write(
        "registry",
        "registry/source=manual/x.parquet",
        [
            {
                "added_at": datetime.now(UTC),
                "source": "manual",
                "roaster_id": "vividcoffee.com",
                "opted_out": True,
            }
        ],
    )
    assert status(lake, "vividcoffee.com").status == "opted_out"


def test_state_codes_from_the_issue_form():
    import pytest

    from coffeedata.discover import state_code

    assert state_code("or") == "OR"
    assert state_code("Oregon") == "OR"
    assert state_code("Washington, DC") == "DC"
    assert state_code("D.C.") == "DC"
    assert state_code("Puerto Rico") == "PR"
    assert state_code("Washington") == "WA"
    assert state_code("  ") == ""
    for bad in ("Ontario", "XX", "Pu"):
        with pytest.raises(ValueError):
            state_code(bad)


def test_a_probe_without_robots_txt_is_not_an_answer(lake):
    rid = "onyxcoffeelab.com"
    probe(lake, "2026-11-01", rid)
    probe(lake, "2026-11-08", rid, status="robots_unavailable", platform="")
    s = status(lake, rid)
    # last_probed is what stale-probe selection reads: the roaster is probed again next run, not in 28 days
    assert s.last_probed == "2026-11-01" and s.roaster.platform == "shopify"


def test_host_key_is_one_store_whatever_the_case():
    # Jones (eval/roaster_dedup): jonescoffee.com resolves to www.Jonescoffeeroasters.com, a capital J.
    assert registry.host_key("www.Jonescoffeeroasters.com") == registry.host_key("jonescoffeeroasters.com")
    assert registry.host_key("shop.magnumcoffee.com:443") == "magnumcoffee.com"


def test_the_probe_names_domains_that_serve_an_active_roasters_store():
    from coffeedata.models import Roaster

    def state(rid, host, status="active"):
        return registry.RoasterState(Roaster(roaster_id=rid, name=rid, host=host), status, None)

    states = [
        state("wonderstate.com", "wonderstate.com"),
        state("thecafeconnection.com", "www.coffeemasters.shop"),
        state("deadshop.com", "expireddomains.com", "no_catalog"),
        state("bluechipcoffeeroaster.com", "bluechipcoffeeroaster.com"),
    ]
    rows = [
        {"roaster_id": "kickapoocoffee.com", "status": "ok", "final_host": "WonderState.com"},  # a rebrand
        {
            "roaster_id": "coffeemasters.shop",
            "status": "ok",
            "final_host": "www.coffeemasters.shop",
        },  # a newcomer
        {"roaster_id": "wonderstate.com", "status": "ok", "final_host": "wonderstate.com"},  # itself
        {"roaster_id": "parked.com", "status": "ok", "final_host": "expireddomains.com"},  # a parked domain
        {
            "roaster_id": "piatanoirroast.com",
            "status": "ok",
            "final_host": "piatanoirroast.com",
        },  # white label
        {"roaster_id": "down.com", "status": "unreachable", "final_host": ""},
    ]
    assert registry.same_store(rows, states) == {
        "coffeemasters.shop": "thecafeconnection.com",
        "kickapoocoffee.com": "wonderstate.com",
    }


def test_an_alias_is_never_crawled():
    from coffeedata import ingest

    assert "alias" not in ingest.INGESTABLE
