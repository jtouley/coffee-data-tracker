"""Shopify limits by client IP across all its stores (run 2026-10-28: 920 Shopify fetches failed on 429s), so
every Shopify store shares one request budget, and a 429 from any of them pauses them all."""

import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
import respx

from coffeedata import discover, http, ingest
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
    assert lim.stats["cooldowns"] == 1 and lim.interval == 0.5  # one cooldown, one halving


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


def test_simultaneous_429s_are_one_block_and_halve_the_rate_once():
    clock = FakeClock()
    lim = limiter(clock, rate=4.0)
    for _ in range(8):  # every request in flight answered 429 together
        lim.record(resp(429, "5"))
    assert lim.interval == 0.5
    assert lim.stats == {"requests": 0, "rate_limited": 8, "cooldowns": 1}
    with lim.slot():
        assert clock.t == pytest.approx(1005.0)
        lim.record(resp(429, "5"))  # the next block, after the cooldown, halves it again
    assert lim.interval == 1.0 and lim.stats["cooldowns"] == 2


def test_a_cooldown_voids_reservations_made_before_it():
    clock = FakeClock()
    lim = limiter(clock, rate=4.0)
    lim._next = clock.t + 30  # as if a queue of waiters had reserved the next 30s
    lim.record(resp(429, "5"))
    with lim.slot():
        assert clock.t == pytest.approx(1005.0)


def test_430_and_503_block_like_429_and_other_errors_are_neutral():
    clock = FakeClock()
    lim = limiter(clock, rate=4.0)
    lim.record(resp(430))  # Shopify's IP-wide security block
    assert lim.cooldown_until == pytest.approx(1020.0) and lim.interval == 0.5
    lim.record(resp(403))
    lim.record(resp(500))
    assert lim.interval == 0.5 and lim._blocked_since == 1000.0  # neither counts as a success
    clock.t = 1020.0
    lim.record(resp(503, "4"))
    assert lim.cooldown_until == pytest.approx(1024.0)
    assert lim.stats == {"requests": 0, "rate_limited": 2, "cooldowns": 2}
    lim.record(resp(404))  # a missing page is a real answer: the platform let it through
    assert lim._blocked_since is None and lim.interval < 1.0


def test_given_up_platform_probes_and_recovers():
    clock = FakeClock()
    lim = limiter(clock, give_up_after=60, probe_every=30)
    lim.record(resp(429, "10"))
    clock.t += 61
    with pytest.raises(RateLimited), lim.slot():  # tripped: fail fast
        pass
    clock.t += 10
    with pytest.raises(RateLimited), lim.slot():
        pass
    clock.t += 20
    with lim.slot():  # the probe
        lim.record(resp(429, "10"))  # still blocked
    with pytest.raises(RateLimited), lim.slot():  # still open: no second request
        pass
    clock.t += 30
    with lim.slot():  # the next probe
        lim.record(resp(200))
    for _ in range(3):  # closed again: everyone goes, at the platform rate
        with lim.slot():
            lim.record(resp(200))
    assert lim.stats["requests"] == 5


def test_slots_freed_together_do_not_release_a_burst():
    lim = PlatformLimiter("shopify", rate=10, max_concurrent=2)
    release, lock = threading.Event(), threading.Lock()
    starts: list[float] = []

    def hold():
        with lim.slot():
            release.wait()

    def one():
        with lim.slot(), lock:
            starts.append(time.monotonic())

    holders = [threading.Thread(target=hold) for _ in range(2)]
    for t in holders:
        t.start()
    time.sleep(0.3)  # both slots held
    queued = [threading.Thread(target=one) for _ in range(4)]
    for t in queued:
        t.start()
    time.sleep(0.6)  # long enough to bank several intervals, had the queue passed the rate gate first
    release.set()
    for t in holders + queued:
        t.join()
    starts.sort()
    assert min(b - a for a, b in zip(starts, starts[1:], strict=False)) >= 0.08  # 1/rate, give or take


def test_a_due_probe_during_a_cooldown_is_claimed_by_one_caller_and_the_rest_fail_fast():
    lim = PlatformLimiter("shopify", rate=100, max_concurrent=8, give_up_after=0, probe_every=0.01)
    lim.record(resp(429, "0.5"))  # tripped at once, with a cooldown still running when the probe is due
    with pytest.raises(RateLimited), lim.slot():  # the first caller after the trip schedules the probe
        pass
    time.sleep(0.05)  # the probe is due; the cooldown has ~0.45s left
    sent, failed, lock = [], [], threading.Lock()

    def one():
        try:
            with lim.slot(), lock:
                sent.append(time.monotonic())
        except RateLimited:
            with lock:
                failed.append(time.monotonic())

    threads = [threading.Thread(target=one) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sent) == 1 and len(failed) == 31
    assert sent[0] >= lim.cooldown_until  # the probe waited out the cooldown
    assert max(failed) < lim.cooldown_until  # nobody else did, holding a slot, only to fail after it


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


def test_a_430_pauses_the_platform_without_the_client_sleeping_too(mock, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    clock = FakeClock()
    lim = limiter(clock, rate=1000)
    a = mock.get("https://a.com/products.json", params={"page": "1"})
    a.side_effect = [resp(430, "9"), _page(range(3))]
    mock.get("https://a.com/products.json", params={"page": "2"}).mock(return_value=_page([]))
    with PoliteClient(min_interval=0, limiters={"shopify": lim}) as c:
        assert len(fetch_catalog(c, _r("a.com"))) == 3
    assert clock.t >= 1009  # the retry waited out the platform cooldown
    assert slept == []  # and only there: the worker didn't also sleep its own backoff
    assert lim.stats["rate_limited"] == 1 and lim.stats["cooldowns"] == 1


def test_robots_txt_gets_the_retries_and_a_429_waits_out_the_cooldown(no_retry_sleep):
    clock = FakeClock()
    lim = limiter(clock, rate=1000)
    with respx.mock as m:
        robots = m.get("https://a.com/robots.txt")
        robots.side_effect = [resp(429, "5"), httpx.Response(200, text="User-agent: *\nDisallow: /cart")]
        products = m.get("https://a.com/products.json").mock(return_value=_page([]))
        with PoliteClient(min_interval=0, limiters={"shopify": lim}) as base:
            c = base.for_platform("shopify")
            assert c.get("https://a.com/products.json").status_code == 200  # within one get()
            with pytest.raises(http.Blocked):  # the rules it got apply
                c.get("https://a.com/cart")
        assert robots.call_count == 2 and products.call_count == 1
    assert clock.t >= 1005  # the retry waited out the cooldown the 429 started


def test_robots_txt_that_never_answers_fails_fast_then_is_asked_again(monkeypatch, no_retry_sleep):
    now = [5000.0]
    monkeypatch.setattr(http.time, "monotonic", lambda: now[0])
    with respx.mock as m:
        robots = m.get("https://a.com/robots.txt")
        robots.side_effect = [resp(500)] * 3 + [httpx.Response(404)]
        products = m.get("https://a.com/products.json").mock(return_value=_page([]))
        with PoliteClient(min_interval=0) as c:  # any client, not only a limited one
            with pytest.raises(http.RobotsUnavailable, match="answered 500") as e:
                c.get("https://a.com/products.json")
            assert isinstance(e.value, httpx.HTTPError) and not isinstance(e.value, http.Blocked)
            assert robots.call_count == 3  # the client's retries
            now[0] += 60
            with pytest.raises(http.RobotsUnavailable, match=r"answered 500 \(recently\)"):
                c.get("https://a.com/products.json")  # remembered: no new robots.txt request
            assert robots.call_count == 3 and products.call_count == 0  # nothing fetched without the rules
            now[0] += http.ROBOTS_RETRY_AFTER
            assert c.get("https://a.com/products.json").status_code == 200  # asked again: now no robots.txt
        assert robots.call_count == 4 and products.call_count == 1


def test_a_probe_without_robots_txt_records_a_status_the_next_run_retries(no_retry_sleep):
    with respx.mock as m:
        m.get(url__regex=r"https://(www\.)?a\.com/robots.txt").mock(return_value=resp(503))
        home = m.get(url__regex=r"https://(www\.)?a\.com/$").mock(return_value=httpx.Response(200))
        with PoliteClient(min_interval=0, retries=2) as c:
            assert discover.probe_one(c, "a.com")["status"] == "robots_unavailable"  # not "unreachable"
        assert home.call_count == 0


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
    monkeypatch.setattr(ingest, "select_states", lambda *a, **k: ([], "full"))
    monkeypatch.setattr(
        ingest, "run", lambda lake, todo, rd, workers, mode, shopify_rps: seen.update(rps=shopify_rps)
    )
    monkeypatch.setattr(cli, "_print", lambda x: None)
    cli.main(["ingest", "--shopify-rps", "1.5"])
    assert seen == {"rps": 1.5}
    cli.main(["ingest"])
    assert seen == {"rps": ingest.SHOPIFY_RPS}  # the weekly run's ingests use the default
    for bad in ("0", "-1", "fast"):
        with pytest.raises(SystemExit):  # a usage error before anything runs, not a quiet fallback
            cli.main(["ingest", "--shopify-rps", bad])


def test_robots_txt_that_cannot_be_fetched_stops_the_crawl_too(no_retry_sleep):
    """A timeout or other network error on robots.txt is no answer about the rules either."""
    with respx.mock as m:
        m.get("https://a.com/robots.txt").mock(side_effect=httpx.ReadTimeout("slow"))
        products = m.get("https://a.com/products.json").mock(return_value=_page([]))
        with (
            PoliteClient(min_interval=0) as c,
            pytest.raises(http.RobotsUnavailable, match="ReadTimeout: slow") as e,
        ):
            c.get("https://a.com/products.json")
        assert not e.value.unreachable and products.call_count == 0
        assert isinstance(e.value.__cause__, httpx.ReadTimeout)  # the original error is kept


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("blackholed")])
def test_a_probe_of_a_host_that_is_down_is_unreachable_not_robots_unavailable(no_retry_sleep, error):
    with respx.mock as m:
        m.get(url__regex=r"https://(www\.)?a\.com/robots\.txt").mock(side_effect=error)
        with PoliteClient(min_interval=0) as c:
            assert discover.probe_one(c, "a.com")["status"] == "unreachable"


def test_a_robots_txt_redirect_to_a_dead_host_does_not_make_the_site_unreachable(no_retry_sleep):
    with respx.mock as m:
        m.get(url__regex=r"https://(www\.)?a\.com/robots\.txt").mock(
            return_value=httpx.Response(301, headers={"location": "https://cdn.dead.example/robots.txt"})
        )
        m.get("https://cdn.dead.example/robots.txt").mock(side_effect=httpx.ConnectError("refused"))
        with PoliteClient(min_interval=0) as c:
            assert discover.probe_one(c, "a.com")["status"] == "robots_unavailable"  # asked again next run


def test_our_own_pool_timing_out_is_not_remembered_against_the_host(no_retry_sleep):
    with respx.mock as m:
        robots = m.get("https://a.com/robots.txt")
        robots.side_effect = [httpx.PoolTimeout("busy")] * 3 + [httpx.Response(404)]
        m.get("https://a.com/products.json").mock(return_value=_page([]))
        with PoliteClient(min_interval=0) as c:
            with pytest.raises(http.RobotsUnavailable, match="not sent") as e:
                c.get("https://a.com/products.json")
            assert not e.value.unreachable
            assert c.get("https://a.com/products.json").status_code == 200  # asked again at once


def test_concurrent_first_requests_to_a_host_share_one_robots_txt_request(no_retry_sleep):
    import threading

    with respx.mock as m:

        def slow_robots(request):
            threading.Event().wait(0.1)  # a slow robots.txt: the other requests arrive while it's in flight
            return httpx.Response(404)

        robots = m.get("https://a.com/robots.txt").mock(side_effect=slow_robots)
        m.get("https://a.com/products.json").mock(return_value=_page([]))
        errors: list[BaseException] = []
        with PoliteClient(min_interval=0) as c:
            go = threading.Barrier(8)

            def fetch():
                try:
                    go.wait()
                    c.get("https://a.com/products.json")
                except BaseException as e:  # noqa: BLE001 - reported below; a thread's own exception is lost
                    errors.append(e)

            threads = [threading.Thread(target=fetch) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert errors == [] and robots.call_count == 1
