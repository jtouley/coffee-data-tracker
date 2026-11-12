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


def add(lake, *rids):
    """Roasters discovery added to the lake registry (the fixture seed lists six)."""
    from datetime import UTC, datetime

    lake.write(
        "registry",
        f"registry/source=overture/{rids[0]}.parquet",
        [
            {"added_at": datetime(2026, 10, 1, tzinfo=UTC), "source": "overture", "roaster_id": r, "name": r}
            for r in rids
        ],
    )


HOSTS = [
    "www.Jonescoffeeroasters.com",  # Jones (eval/roaster_dedup): a capital J
    "jonescoffeeroasters.com",
    "shop.magnumcoffee.com:443",
    "https://WWW.Example.com/shop?x=1",
    "http://store.example.com.",
    ".www2.example.com",
    "locations.dutchbros.com",
    "Example.COM/path/to",
    "north-fork-coffee-roasters.myshopify.com",
    " www.example.com ",
    "shop.example.com?ref=1",
    "example.com:8080/x",
]


def test_host_key_is_one_store_whatever_the_case():
    assert registry.host_key("www.Jonescoffeeroasters.com") == registry.host_key("jonescoffeeroasters.com")
    assert registry.host_key("shop.magnumcoffee.com:443") == "magnumcoffee.com"


@pytest.mark.parametrize("host", HOSTS)
def test_the_sql_host_key_is_registry_host_key(host):
    """One rule (review of #137, L3): dbt's host_key macro, which STATE_SQL renders too, gives registry.host_key's
    key (normalize_domain) for every spelling of a host."""
    import duckdb

    sql = registry.alias_macros().host_key("?")
    got = duckdb.connect().execute(f"select {sql}", [host]).fetchall()[0][0]
    assert got == registry.host_key(host)


def test_shared_hosts_are_one_list():
    """dbt's seed shared_hosts is registry's list: NOT_A_ROASTER_SITE is its not_a_roaster rows."""
    rows = registry._shared_hosts()
    assert {r["kind"] for r in rows} <= {"not_a_roaster", "platform", "parked", "shortener", "directory"}
    assert len({r["host"] for r in rows}) == len(rows)
    assert {"facebook.com", "orderspoon.com"} <= set(registry.NOT_A_ROASTER_SITE)
    assert (
        "hugedomains.com" not in registry.NOT_A_ROASTER_SITE
    )  # parked: never groups, but discovery may list it


def test_the_probe_names_domains_that_became_aliases(lake):
    """The probe's same_store report is the crawl's own rule (registry.current after the probe, review of #137 M5): a
    rebrand and a newcomer that resolve to a store are listed; a roaster that is itself, a parked domain, a white
    label and an alias already known before the probe are not."""
    add(lake, "wonderstate.com", "bluechipcoffeeroaster.com", "jonescoffee.com", "jonescoffeeroasters.com",
        "kickapoocoffee.com", "parked.com", "deadshop.com", "piatanoirroast.com")  # fmt: skip
    run(lake, "2026-11-02", "wonderstate.com", [product("1")])
    run(lake, "2026-11-02", "bluechipcoffeeroaster.com", [product("1")])
    probe(lake, "2026-11-01", "jonescoffee.com", final_host="www.jonescoffeeroasters.com")
    probe(lake, "2026-11-01", "jonescoffeeroasters.com", final_host="jonescoffeeroasters.com")
    before = registry.current(lake, SEED)
    probe(lake, "2026-11-08", "kickapoocoffee.com", final_host="WonderState.com")  # a rebrand
    probe(lake, "2026-11-08", "wonderstate.com", final_host="wonderstate.com")  # itself
    probe(lake, "2026-11-08", "parked.com", final_host="expireddomains.com")  # a parked domain
    probe(lake, "2026-11-08", "deadshop.com", final_host="www.expireddomains.com")
    probe(lake, "2026-11-08", "piatanoirroast.com", final_host="piatanoirroast.com")  # white label
    probe(lake, "2026-11-08", "jonescoffee.com", final_host="www.jonescoffeeroasters.com")  # known alias
    after = registry.current(lake, SEED)
    probed = ["kickapoocoffee.com", "wonderstate.com", "parked.com", "deadshop.com", "piatanoirroast.com",
              "jonescoffee.com"]  # fmt: skip
    assert registry.same_store(probed, before, after) == {"kickapoocoffee.com": "wonderstate.com"}


def test_a_dropped_roaster_never_groups(lake):
    """A roaster the caller drops (the admin's "not a roaster" flag, review of #137 M1) is left out before the stores
    are decided: its other domain is its own store, not an alias of a roaster that isn't there."""
    add(lake, "wonderstate.com", "kickapoocoffee.com")
    run(lake, "2026-11-02", "wonderstate.com", [product("1")])
    run(lake, "2026-11-02", "kickapoocoffee.com", [product("1")])
    probe(lake, "2026-11-01", "kickapoocoffee.com", final_host="wonderstate.com")
    states = {s.roaster.roaster_id: s for s in registry.current(lake, SEED)}
    assert (states["kickapoocoffee.com"].status, states["kickapoocoffee.com"].canonical_roaster_id) == (
        "alias",
        "wonderstate.com",
    )
    states = {s.roaster.roaster_id: s for s in registry.current(lake, SEED, dropped={"wonderstate.com"})}
    assert "wonderstate.com" not in states
    assert (states["kickapoocoffee.com"].status, states["kickapoocoffee.com"].canonical_roaster_id) == (
        "active",
        "kickapoocoffee.com",
    )


def test_only_and_a_retry_never_crawl_an_alias(lake):
    """`--only` ("Pull now") and a roaster already tried this run never crawl an alias (review of #137, L6)."""
    from coffeedata import ingest

    add(lake, "wonderstate.com", "kickapoocoffee.com")
    run(lake, "2026-11-02", "wonderstate.com", [product("1")])
    probe(lake, "2026-11-01", "kickapoocoffee.com", final_host="wonderstate.com")
    states, mode = ingest.select_states(lake, "2026-11-09", only="kickapoocoffee.com")
    assert (states, mode) == ([], "only")
    alias = next(s for s in registry.current(lake) if s.roaster.roaster_id == "kickapoocoffee.com")
    assert not ingest.crawlable(alias, {"kickapoocoffee.com"})


def test_an_alias_is_never_crawled():
    from coffeedata import ingest

    assert "alias" not in ingest.INGESTABLE
