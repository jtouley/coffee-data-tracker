"""Shopify limits by client IP across all its stores (run 2026-10-28: 920 Shopify fetches failed on 429s), so
every Shopify store shares one request budget, and a 429 from any of them pauses them all."""

import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
import respx

from coffeedata import http, ingest
from coffeedata.adapters import fetch_catalog
from coffeedata.http import FetchError, PoliteClient
from coffeedata.models import Roaster
from coffeedata.ratelimit import PlatformLimiter, RateLimited, retry_after_seconds


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.sleeps.append(round(d, 6))
        self.t += d


def limiter(clock: FakeClock, rate=4.0, **kw) -> PlatformLimiter:
    return PlatformLimiter("shopify", rate=rate, max_concurrent=8, clock=clock, sleep=clock.sleep, **kw)


def resp(status: int, retry_after: str | None = None) -> httpx.Response:
    return httpx.Response(status, headers={"retry-after": retry_after} if retry_after is not None else {})


def _r(host: str, platform: str = "shopify") -> Roaster:
    return Roaster(roaster_id=host, name="R", host=host, platform=platform)


def _page(ids):
    return httpx.Response(
        200, json={"products": [{"id": i, "title": f"P{i}", "handle": f"p{i}", "variants": []} for i in ids]}
    )


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://[^/]+/robots.txt").mock(return_value=httpx.Response(404))
        yield m


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """PoliteClient's own per-request backoff, separate from the limiter's (fake) clock."""
    monkeypatch.setattr(http.time, "sleep", lambda s: None)


# --- the limiter itself, on a fake clock -------------------------------------------------------------


def test_requests_are_spaced_at_the_platform_rate():
    clock = FakeClock()
    lim = limiter(clock, rate=4.0)
    starts = []
    for _ in range(5):
        with lim.slot():
            starts.append(clock.t)
        lim.record(resp(200))
    assert [round(b - a, 6) for a, b in zip(starts, starts[1:], strict=False)] == [0.25] * 4
    assert lim.stats["requests"] == 5


def test_a_429_pauses_the_whole_platform_for_retry_after_and_slows_it():
    clock = FakeClock()
    lim = limiter(clock, rate=4.0)
    with lim.slot():
        lim.record(resp(429, "7"))
    assert lim.interval == 0.5  # half the rate
    with lim.slot():  # any store's next request waits out the cooldown
        assert clock.t == pytest.approx(1007.0)
        lim.record(resp(200))
    assert lim.stats == {"requests": 2, "rate_limited": 1, "cooldowns": 1}
    for _ in range(200):  # successes bring the rate back, never above the configured one
        with lim.slot():
            lim.record(resp(200))
    assert lim.interval == lim.base_interval == 0.25


def test_cooldown_without_retry_after_is_the_default_and_is_capped():
    clock = FakeClock()
    lim = limiter(clock, default_cooldown=20, max_cooldown=120)
    lim.record(resp(429))
    assert lim.cooldown_until == pytest.approx(clock.t + 20)
    lim.record(resp(429, "3600"))
    assert lim.cooldown_until == pytest.approx(clock.t + 120)
    lim.record(resp(429, "1"))  # a shorter Retry-After never cuts a cooldown short
    assert lim.cooldown_until == pytest.approx(clock.t + 120)
    assert lim.stats["cooldowns"] == 1 and lim.interval == lim.max_interval == 2.0  # 4 rps / 8


def test_retry_after_forms():
    now = datetime(2026, 10, 28, 17, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds("Wed, 28 Oct 2026 17:00:30 GMT", now=now) == 30
    assert retry_after_seconds("Wed, 28 Oct 2026 16:00:00 GMT", now=now) == 0
    assert retry_after_seconds("soon") is None and retry_after_seconds("") is None
    assert retry_after_seconds(None) is None


def test_gives_up_when_only_429s_for_too_long():
    clock = FakeClock()
    lim = limiter(clock, give_up_after=600)
    for _ in range(40):
        try:
            with lim.slot():
                lim.record(resp(429, "30"))
        except RateLimited as e:
            assert isinstance(e, FetchError)  # recorded as the roaster's fetch error, like any other
            break
    else:
        pytest.fail("never gave up")
    assert 600 <= clock.t - 1000 < 700
    with pytest.raises(RateLimited), lim.slot():  # the rest fail fast instead of waiting their own turn
        pass


def test_a_success_resets_the_give_up_timer():
    clock = FakeClock()
    lim = limiter(clock, give_up_after=60)
    for _ in range(10):
        with lim.slot():
            lim.record(resp(429, "20"))
        with lim.slot():
            lim.record(resp(200))
    assert clock.t - 1000 > 60


def test_concurrent_requests_are_capped():
    lim = PlatformLimiter("shopify", rate=10_000, max_concurrent=3)
    active, peak, lock = 0, 0, threading.Lock()

    def one():
        nonlocal active, peak
        with lim.slot():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=one) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 3 and lim.stats["requests"] == 12


# --- through PoliteClient and the adapters -------------------------------------------------------------


def test_a_429_on_one_store_pauses_the_others_and_both_catalogs_complete(mock, no_retry_sleep):
    clock = FakeClock()
    lim = limiter(clock, rate=1000)
    a = mock.get("https://a.com/products.json", params={"page": "1"})
    a.side_effect = [resp(429, "9"), _page(range(3))]
    mock.get("https://a.com/products.json", params={"page": "2"}).mock(return_value=_page([]))
    mock.get("https://b.com/products.json", params={"page": "1"}).mock(return_value=_page(range(10, 15)))
    mock.get("https://b.com/products.json", params={"page": "2"}).mock(return_value=_page([]))
    with PoliteClient(min_interval=0, limiters={"shopify": lim}) as c:
        assert len(fetch_catalog(c, _r("a.com"))) == 3
        t = clock.t
        assert t >= 1009  # a.com's retry waited out the cooldown its 429 started
        assert len(fetch_catalog(c, _r("b.com"))) == 5
    assert lim.stats["rate_limited"] == 1 and lim.stats["cooldowns"] == 1
    # robots.txt for both stores, a.com's two tries at page 1, page 2, and b.com's two pages
    assert lim.stats["requests"] == 2 + 2 + 1 + 2


def test_store_cooldown_blocks_another_store_mid_cooldown(mock, no_retry_sleep):
    clock = FakeClock()
    lim = limiter(clock, rate=1000)
    mock.get("https://b.com/products.json").mock(return_value=_page([]))
    with PoliteClient(min_interval=0, limiters={"shopify": lim}) as c:
        lim.record(resp(429, "15"))  # as if a.com, on another worker, just got a 429
        assert fetch_catalog(c, _r("b.com")) == []
    assert clock.t >= 1015  # b.com is a different host, but the same platform


def test_other_platforms_keep_per_host_politeness_only(mock, no_retry_sleep):
    clock = FakeClock()
    lim = limiter(clock, rate=1000)
    lim.record(resp(429, "60"))  # Shopify is cooling down
    mock.get("https://w.com/wp-json/wc/store/v1/products").mock(return_value=httpx.Response(200, json=[]))
    with PoliteClient(min_interval=0, limiters={"shopify": lim}) as c:
        assert c.for_platform("woocommerce") is c
        assert fetch_catalog(c, _r("w.com", "woocommerce")) == []
        # a Shopify-hosted domain on another adapter is that adapter's: the adapter decides, not the hostname
        assert c.for_platform("generic") is c
    assert clock.t == 1000 and lim.stats["requests"] == 0


def test_persistent_429_is_a_fetch_error_and_no_partial_catalog_is_stored(lake, mock):
    mock.get("https://onyx.com/products.json", params={"page": "1"}).mock(return_value=_page(range(250)))
    mock.get("https://onyx.com/products.json", params={"page": "2"}).mock(return_value=resp(429, "0"))
    out = ingest.run(lake, [_r("onyx.com")], "2026-10-26", workers=1, shopify_rps=1000)
    assert out["by_adapter"]["shopify"]["errors"] == 1
    assert "FetchError: HTTPStatusError" in out["failures"][0]
    assert out["shopify_limiter"]["rate_limited"] == 3  # every retry of page 2
    con = lake.connect()
    assert con.execute(f"select status from {lake.scan('fetches')}").fetchall() == [("error",)]
    assert con.execute(f"select count(*) from {lake.scan('snapshots')}").fetchone()[0] == 0


def test_given_up_platform_fails_fast_as_fetch_error(mock):
    clock = FakeClock()
    lim = limiter(clock, give_up_after=60)
    lim.record(resp(429, "30"))
    clock.t += 61
    with (
        PoliteClient(min_interval=0, limiters={"shopify": lim}) as c,
        pytest.raises(FetchError, match="giving up"),
    ):
        fetch_catalog(c, _r("a.com"))
    assert not mock.calls  # not even robots.txt was asked for


def test_cli_shopify_rps_reaches_the_run(lake, monkeypatch):
    from coffeedata import cli

    seen = {}
    monkeypatch.setattr(ingest, "select", lambda *a, **k: [])
    monkeypatch.setattr(
        ingest, "run", lambda lake, todo, rd, workers, shopify_rps: seen.update(rps=shopify_rps)
    )
    monkeypatch.setattr(cli, "_print", lambda x: None)
    cli.main(["ingest", "--shopify-rps", "1.5"])
    assert seen == {"rps": 1.5}
    cli.main(["ingest"])
    assert seen == {"rps": ingest.SHOPIFY_RPS}  # the weekly run's ingests use the default
