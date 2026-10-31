import threading

from coffeedata import ingest
from coffeedata.models import Roaster


def _r(rid: str, platform: str) -> Roaster:
    return Roaster(roaster_id=rid, name=rid, host=rid, platform=platform)


def test_a_stuck_platform_does_not_hold_up_the_others(monkeypatch):
    """Every Shopify fetch waits (a limiter in cooldown); the WooCommerce roasters still all finish first."""
    release = threading.Event()

    def one(client, roaster):
        if roaster.adapter == "shopify":
            assert release.wait(10)
        return ingest.Result(roaster, None)  # type: ignore[arg-type]

    monkeypatch.setattr(ingest, "ingest_one", one)
    roasters = [_r(f"s{i}.com", "shopify") for i in range(8)] + [
        _r(f"w{i}.com", "woocommerce") for i in range(20)
    ]
    order = []
    for res in ingest.crawl(None, roasters, {"shopify": 4, "woocommerce": 2}):  # type: ignore[arg-type]
        order.append(res.roaster.adapter)
        if len(order) == 20:
            release.set()
    assert order == ["woocommerce"] * 20 + ["shopify"] * 8


def test_a_total_worker_count_scales_every_pool():
    pools = {a: max(1, round(n * 16 / sum(ingest.POOLS.values()))) for a, n in ingest.POOLS.items()}
    assert set(pools) == set(ingest.POOLS) and all(n >= 1 for n in pools.values())
