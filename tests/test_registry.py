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
