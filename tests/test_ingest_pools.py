import random
import threading

import pytest

from coffeedata import ingest
from coffeedata.models import Roaster
from coffeedata.ratelimit import SHOPIFY_CONCURRENCY


def _r(rid: str, platform: str) -> Roaster:
    return Roaster(roaster_id=rid, name=rid, host=rid, platform=platform)


@pytest.mark.parametrize("seed", range(5))
def test_a_stuck_platform_does_not_hold_up_the_others(monkeypatch, seed):
    """Every Shopify fetch waits (a limiter in cooldown); in any submission order, the WooCommerce roasters still
    all finish before any Shopify one."""
    release = threading.Event()

    def one(client, roaster):
        if roaster.adapter == "shopify":
            assert release.wait(10)
        return ingest.Result(roaster, None)  # type: ignore[arg-type]

    monkeypatch.setattr(ingest, "ingest_one", one)
    roasters = [_r(f"s{i}.com", "shopify") for i in range(8)] + [
        _r(f"w{i}.com", "woocommerce") for i in range(20)
    ]
    random.Random(seed).shuffle(roasters)
    order = []
    for res in ingest.crawl(None, roasters, {"shopify": 4, "woocommerce": 2}):  # type: ignore[arg-type]
        order.append(res.roaster.adapter)
        if len(order) == 20:
            release.set()
    assert order == ["woocommerce"] * 20 + ["shopify"] * 8


ALL = set(ingest.POOLS)


@pytest.mark.parametrize("workers", [5, 12, 13, 16, 32, 54, 100])
def test_a_total_worker_count_is_split_exactly_and_keeps_shopify_at_its_limit(workers):
    sizes = ingest.pool_sizes(workers, ALL)
    assert sum(sizes.values()) == workers
    assert set(sizes) == ALL and min(sizes.values()) >= 1
    if workers >= SHOPIFY_CONCURRENCY + len(ALL) - 1:
        assert sizes["shopify"] >= SHOPIFY_CONCURRENCY


def test_only_the_run_s_adapters_get_pools():
    assert ingest.pool_sizes(1, {"shopify"}) == {"shopify": 1}
    assert ingest.pool_sizes(16, {"shopify"}) == {"shopify": 16}
    assert ingest.pool_sizes(2, {"generic", "squarespace"}) == {"generic": 1, "squarespace": 1}
    with pytest.raises(ValueError):
        ingest.pool_sizes(1, {"shopify", "generic"})


def test_run_hands_crawl_the_split_of_its_workers(lake, monkeypatch):
    seen = {}

    def crawl(client, roasters, pools):
        seen.update(pools)
        return iter(())

    monkeypatch.setattr(ingest, "crawl", crawl)
    roasters = [
        _r(f"{p}.com", p) for p in ("shopify", "woocommerce", "squarespace?", "square_online?", "wix?")
    ]
    ingest.run(lake, roasters, "2026-10-26", workers=16)
    assert sum(seen.values()) == 16 and seen["shopify"] >= SHOPIFY_CONCURRENCY
