"""Crawl run 2026-11-02 (build/research/crawl-failures-2026-11-02.md): SiteGround's anti-bot answered HTTP 202 with
`sg-captcha: challenge` to robots.txt and to the catalog, and the client read the robots.txt 202 as "no robots.txt".
A challenge on robots.txt (or a 1xx, or a 3xx without a location) now leaves the host unavailable for the pass, as
a 429 or 5xx already did; a plain 4xx still means no robots.txt (RFC 9309). Every challenge is recorded in fetches.blocks with its headers. Also the run's Squarespace burst (one shared budget
now, as Shopify's) and the per-roaster registry corrections for the Square Online stores that moved."""

from datetime import date

import httpx
import pytest
import respx

from coffeedata import connectors, http, ingest, ratelimit, registry
from coffeedata.http import PoliteClient
from coffeedata.models import Roaster
from tests.test_ratelimit import FAST, fake_time

WEEK = "2026-11-02"
# What SiteGround's challenge looked like (third-party reports; our probes from another IP got the 200 behind it).
SG = {"server": "nginx", "sg-captcha": "challenge", "content-type": "text/html; charset=UTF-8"}
SG_BODY = '<html><head><meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=%2F"></head></html>'


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setenv("COFFEEDATA_EGRESS_IP", "203.0.113.9")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)


def _woo(host: str = "thomasandfisk.com") -> Roaster:
    return Roaster(roaster_id=host, name="R", host=host, platform="woocommerce")


# --- robots.txt --------------------------------------------------------------------------------------


def test_a_challenged_robots_txt_is_no_answer_and_the_host_is_left_alone(monkeypatch):
    """The reproduced case: robots.txt answered 202 + sg-captcha. Before, _fetch_robots returned None (no rules)
    and the catalog was asked next; now nothing else is asked of the host, for ROBOTS_RETRY_AFTER."""
    now = [5000.0]
    monkeypatch.setattr(http.time, "monotonic", lambda: now[0])
    with respx.mock as m:
        robots = m.get("https://a.com/robots.txt").mock(
            return_value=httpx.Response(202, headers=SG, text=SG_BODY)
        )
        catalog = m.get("https://a.com/wp-json/wc/store/v1/products").mock(
            return_value=httpx.Response(200, json=[])
        )
        with PoliteClient(min_interval=0) as c:
            with pytest.raises(http.RobotsUnavailable, match=r"answered 202 \(a challenge\)") as e:
                c.get("https://a.com/wp-json/wc/store/v1/products")
            assert e.value.status == 202 and not e.value.unreachable
            assert robots.call_count == 1  # a 202 isn't retried: it answers every retry the same way
            now[0] += 60
            with pytest.raises(http.RobotsUnavailable, match="recently") as again:
                c.get("https://a.com/wp-json/wc/store/v1/products")
            assert again.value.status == 202  # the remembered answer keeps saying it was a challenge
            assert robots.call_count == 1 and catalog.call_count == 0  # nothing fetched without the rules


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(202),  # a 202 without SiteGround's header is still no robots.txt
        httpx.Response(302),  # a redirect without a location: can't be followed, rules undefined (RFC 9309)
        httpx.Response(200, headers=SG, text=SG_BODY),  # a 200 that is SiteGround's challenge page
        httpx.Response(
            200, headers={"cf-mitigated": "challenge", "content-type": "text/html"}, text="<html>"
        ),
        httpx.Response(400, headers=SG),  # a 4xx means no robots.txt, unless it is a challenge
        httpx.Response(429),  # unchanged
        httpx.Response(500),  # unchanged
    ],
    ids=[
        "202",
        "302-no-location",
        "200-sg-captcha",
        "200-cf-challenge",
        "400-sg-captcha",
        "429",
        "500",
    ],
)
def test_a_challenge_or_a_non_answer_leaves_the_host_unavailable(response):
    with respx.mock as m:
        m.get("https://a.com/robots.txt").mock(return_value=response)
        catalog = m.get("https://a.com/products.json").mock(
            return_value=httpx.Response(200, json={"products": []})
        )
        with PoliteClient(min_interval=0, retries=1) as c, pytest.raises(http.RobotsUnavailable):
            c.get("https://a.com/products.json")
        assert catalog.call_count == 0


@pytest.mark.parametrize(
    ("response", "allowed"),
    [
        (httpx.Response(204), True),  # no body: no rules
        (httpx.Response(203, text="User-agent: *\nDisallow: /private\n"), True),
        (httpx.Response(206, text="User-agent: *\nDisallow: /\n"), False),
    ],
    ids=["204", "203-rules", "206-disallow-all"],
)
def test_any_2xx_robots_txt_that_is_not_a_challenge_is_the_file(response, allowed):
    """RFC 9309 2.3.1.1: a 2xx is a successful download and its rules are followed; a 204 has none, so everything is
    allowed. Before, a 204 left the store unavailable every week. A 202 is still a challenge (above)."""
    with respx.mock as m:
        robots = m.get("https://a.com/robots.txt").mock(return_value=response)
        catalog = m.get("https://a.com/products.json").mock(
            return_value=httpx.Response(200, json={"products": []})
        )
        with PoliteClient(min_interval=0, retries=1) as c:
            if allowed:
                assert c.get("https://a.com/products.json").status_code == 200
            else:
                with pytest.raises(http.SiteDisallowed):
                    c.get("https://a.com/products.json")
        assert robots.call_count == 1 and catalog.call_count == int(allowed)


@pytest.mark.parametrize("status", [404, 400, 410, 451])
def test_a_plain_4xx_still_means_no_robots_txt(status):
    """RFC 9309: a 4xx is "unavailable", crawl without restrictions. Square Online's API host, cdn5.editmysite.com,
    answers 400 to /robots.txt (checked 2026-11-02): every Square Online catalog is read through it."""
    url = "https://cdn5.editmysite.com/app/store/api/v28/x"
    with respx.mock as m:
        m.get("https://cdn5.editmysite.com/robots.txt").mock(return_value=httpx.Response(status))
        api = m.get(url).mock(return_value=httpx.Response(200, json={}))
        with PoliteClient(min_interval=0) as c:
            assert c.get(url).status_code == 200
        assert api.call_count == 1


@pytest.mark.parametrize(
    "status, headers",
    [(403, {"cf-mitigated": "challenge"}), (401, SG), (403, SG)],
    ids=["403-cf-challenge", "401-sg-captcha", "403-sg-captcha"],
)
def test_a_401_or_403_challenge_is_no_answer_not_disallow_all(monkeypatch, status, headers):
    """Review item 3: a challenge on a 401/403 robots.txt is no answer about the rules (RobotsUnavailable, asked
    again after ROBOTS_RETRY_AFTER), not a "disallow everything" cached for the client's lifetime."""
    now = [5000.0]
    monkeypatch.setattr(http.time, "monotonic", lambda: now[0])
    with respx.mock as m:
        robots = m.get("https://a.com/robots.txt").mock(return_value=httpx.Response(status, headers=headers))
        m.get("https://a.com/products.json").mock(return_value=httpx.Response(200, json={"products": []}))
        with PoliteClient(min_interval=0, retries=1) as c:
            with pytest.raises(http.RobotsUnavailable, match=r"\(a challenge\)") as e:
                c.get("https://a.com/products.json")
            assert e.value.challenge and e.value.status == status
            assert ingest.blocked(e.value) and not ingest.deferrable(e.value)
            now[0] += http.ROBOTS_RETRY_AFTER + 1
            with pytest.raises(http.RobotsUnavailable):
                c.get("https://a.com/products.json")
        assert robots.call_count == 2  # asked again once ROBOTS_RETRY_AFTER passed


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, headers=SG, text=SG_BODY),
        httpx.Response(200, headers={"cf-mitigated": "challenge"}, text="<html>"),
        httpx.Response(400, headers=SG),
    ],
    ids=["200-sg-captcha", "200-cf-challenge", "400-sg-captcha"],
)
def test_a_header_only_challenge_on_robots_txt_is_a_block_and_a_refused_first_contact(
    lake, monkeypatch, response
):
    """Review item 4: is_challenge and ingest.challenged agree. A robots.txt challenge carried only in a header is a
    block, never deferred, and a first contact it answers counts as refused."""
    fake_time(monkeypatch)
    with respx.mock(assert_all_called=False) as m:
        m.get("https://thomasandfisk.com/robots.txt").mock(return_value=response)
        out = ingest.run(lake, [_woo()], WEEK, workers=1, client_kw=FAST)
    assert out["first_contact"]["refused"] == 1
    assert out["deferred"].get("round_1", 0) == 0  # a challenge is never deferred


def test_401_403_still_disallow_everything_and_a_200_is_the_rules():
    with respx.mock as m:
        m.get("https://open.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://open.com/products.json").mock(return_value=httpx.Response(200, json={"products": []}))
        m.get("https://shut.com/robots.txt").mock(return_value=httpx.Response(403))
        m.get("https://ok.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /c")
        )
        m.get("https://ok.com/products.json").mock(return_value=httpx.Response(200, json={"products": []}))
        with PoliteClient(min_interval=0) as c:
            assert c.get("https://open.com/products.json").status_code == 200
            assert c.get("https://ok.com/products.json").status_code == 200
            with pytest.raises(http.Blocked):
                c.get("https://ok.com/c")
            with pytest.raises(http.Blocked):
                c.get("https://shut.com/products.json")


def test_is_challenge():
    assert http.is_challenge(httpx.Response(202))
    assert http.is_challenge(httpx.Response(200, headers={"sg-captcha": "challenge"}))
    assert http.is_challenge(httpx.Response(403, headers={"cf-mitigated": "challenge"}))
    assert not http.is_challenge(httpx.Response(200, headers={"server": "nginx"}))
    assert not http.is_challenge(httpx.Response(429, headers={"server": "Squarespace"}))


# --- recorded as blocks, counted as refusals, not deferred ---------------------------------------------------


def _blocks(lake) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    rows = lake.connect().execute(f"select roaster_id, blocks from {lake.scan('fetches')}").fetchall()
    for rid, bs in rows:
        out.setdefault(rid, []).extend(bs or [])
    return out


def test_challenges_on_robots_txt_and_on_the_catalog_are_recorded_with_sg_captcha(lake, monkeypatch):
    """Both shapes from the run: 202 on robots.txt (37 stores), and robots.txt fine but the catalog 202
    (mozzaroasters' variations). Each lands in fetches.blocks with its headers; neither is retried in the run's
    deferred rounds; a first contact answered with a challenge counts as a refusal (egress-check)."""
    fake_time(monkeypatch)
    with respx.mock(assert_all_called=False) as m:
        robots = m.get("https://thomasandfisk.com/robots.txt").mock(
            return_value=httpx.Response(202, headers=SG, text=SG_BODY)
        )
        m.get("https://kauairoastery.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *")
        )
        catalog = m.get("https://kauairoastery.com/wp-json/wc/store/v1/products").mock(
            return_value=httpx.Response(202, headers=SG, text=SG_BODY)
        )
        out = ingest.run(lake, [_woo(), _woo("kauairoastery.com")], WEEK, workers=1, client_kw=FAST)
    assert robots.call_count == 1 and catalog.call_count == 1  # asked once each, all run
    blocks = _blocks(lake)
    for rid, path in (
        ("thomasandfisk.com", "/robots.txt"),
        ("kauairoastery.com", "/wp-json/wc/store/v1/products"),
    ):
        [b] = blocks[rid]
        assert (b["status"], b["path"], b["sg_captcha"], b["server"], b["egress_ip"]) == (
            202, path, "challenge", "nginx", "203.0.113.9")  # fmt: skip
    errors = dict(lake.connect().execute(f"select roaster_id, error from {lake.scan('fetches')}").fetchall())
    assert "RobotsUnavailable" in errors["thomasandfisk.com"]  # failed, never read as "no robots.txt"
    assert "HTTP 202" in errors["kauairoastery.com"]
    assert out["first_contact"]["refused"] == 2  # visible to egress-check now
    assert out["deferred"].get("still_failing", 0) == 0  # never parked for a deferred round


def test_a_challenge_is_a_block_but_never_deferrable():
    e = http.FetchError("HTTP 202 https://a.com/wp-json/wc/store/v1/products", status=202)
    assert ingest.blocked(e) and not ingest.deferrable(e)
    r = http.RobotsUnavailable("robots.txt for a.com answered 202 (a challenge)", status=202)
    wrapped = http.FetchError("RobotsUnavailable https://a.com/sitemap.xml")
    wrapped.__cause__ = r
    assert ingest.blocked(wrapped) and not ingest.deferrable(wrapped)
    # unchanged: a 429 is a block worth deferring; a robots.txt 500 is neither
    assert ingest.deferrable(http.FetchError("HTTP 429", status=429))
    assert not ingest.blocked(http.RobotsUnavailable("robots.txt for a.com answered 500"))


def test_a_challenged_sitemap_is_a_block():
    """generic's 29 `sitemap HTTP 202` rows: the sitemap error now carries its status."""
    from coffeedata.sitemap import sitemap_urls

    with respx.mock as m:
        m.get("https://a.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://a.com/sitemap.xml").mock(return_value=httpx.Response(202, headers=SG, text=SG_BODY))
        with PoliteClient(min_interval=0) as c, pytest.raises(http.FetchError, match="sitemap HTTP 202") as e:
            sitemap_urls(c, "a.com")
    assert e.value.status == 202 and ingest.blocked(e.value)


# --- Squarespace's shared budget -------------------------------------------------------------------------


def test_squarespace_has_a_shared_budget_like_shopify():
    sq = connectors.CONNECTORS["squarespace"]
    assert sq.rps == ratelimit.SQUARESPACE_RPS == 2.0
    assert connectors.CONNECTORS["shopify"].rps == ratelimit.SHOPIFY_RPS  # unchanged
    assert all(connectors.CONNECTORS[n].rps is None for n in ("woocommerce", "square_online", "generic"))


def test_the_squarespace_job_sends_every_request_through_its_limiter(lake, monkeypatch):
    """crawl --connector squarespace builds a PlatformLimiter for squarespace at SQUARESPACE_RPS, and the adapter's
    requests (robots.txt included) count against it."""
    made: dict[str, ratelimit.PlatformLimiter] = {}
    real = ingest.PlatformLimiter

    def spy(name, **kw):
        made[name] = real(name, **kw)
        return made[name]

    monkeypatch.setattr(ingest, "PlatformLimiter", spy)
    store = Roaster(roaster_id="787coffee.com", name="R", host="787coffee.com", platform="squarespace?")
    monkeypatch.setattr(
        registry, "current", lambda lake, *a, **k: [registry.RoasterState(store, "active", None)]
    )
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://787coffee\.com/.*").mock(return_value=httpx.Response(404))
        out = connectors.crawl(lake, "squarespace", WEEK)
    assert out["rps"] == 2.0
    sq = made["squarespace"]
    assert sq.base_interval == 0.5 and sq.stats["requests"] >= 1


def test_every_platform_limiter_is_set_up_like_shopifys(lake, monkeypatch):
    """Review item 1: burst counting off for a retry pass, the run's earlier blocks known, and (below) no deferring
    in the deferred rounds: the same for Squarespace's limiter as for Shopify's."""
    made: dict[str, ratelimit.PlatformLimiter] = {}
    real = ingest.PlatformLimiter

    def spy(name, **kw):
        made[name] = real(name, **kw)
        return made[name]

    monkeypatch.setattr(ingest, "PlatformLimiter", spy)
    monkeypatch.setattr(ingest, "blocked_hosts", lambda lake, run_date: {"earlier.com"})
    ingest.run(lake, [], WEEK, mode="retry_failed", platform_rps={"squarespace": 2.0})
    assert set(made) == {"shopify", "squarespace"}
    for lim in made.values():
        assert lim.count_bursts is False and "earlier.com" in lim.ever_blocked


def test_the_squarespace_limiter_waits_in_the_deferred_rounds(lake, monkeypatch):
    """A 429 without Retry-After (a 20 s pause, over defer_after) defers the store in the main pass; in the deferred
    round the worker waits each new pause out instead of raising HostRateLimited at once."""
    from tests.test_ratelimit import FakeTime

    clock = FakeTime()
    monkeypatch.setattr(ingest, "time", clock)
    made: dict[str, ratelimit.PlatformLimiter] = {}

    def spy(name, **kw):
        made[name] = ratelimit.PlatformLimiter(name, clock=clock.monotonic, sleep=clock.sleep, **kw)
        return made[name]

    monkeypatch.setattr(ingest, "PlatformLimiter", spy)
    store = Roaster(roaster_id="q.com", name="R", host="q.com", platform="squarespace")
    item = {"id": "a1", "title": "Blend", "urlId": "blend", "variants": [{"price": 1800}]}
    with respx.mock(assert_all_called=False) as m:
        m.get("https://q.com/robots.txt").mock(return_value=httpx.Response(404))
        m.get("https://q.com/sitemap.xml").mock(return_value=httpx.Response(404))
        m.get("https://q.com/shop").mock(
            side_effect=[*[httpx.Response(429)] * 3, httpx.Response(200, json={"items": [item]})]
        )
        out = ingest.run(lake, [store], WEEK, workers=1, platform_rps={"squarespace": 2.0}, client_kw=FAST)
    sq = made["squarespace"]
    # one round: before, the deferred rounds raised at its first 429 each, and three 429s used them all up
    assert out["deferred"] == {"round_1": 1, "still_failing": 0, "refused": 0}
    assert sq.defer is False and sq.stats["rate_limited"] == 3
    assert out["limiters"]["squarespace"] == dict(sq.stats)


def test_a_challenge_never_speeds_the_limiter_up():
    """Review item 6: a 202 challenge (or a 200 with a challenge header) is no success: the limiter's rate and its
    blocked state stay as they were."""
    lim = ratelimit.PlatformLimiter("squarespace", rate=2.0, max_concurrent=8, per_host=True)
    lim.interval = 4.0
    for r in (httpx.Response(202, headers=SG), httpx.Response(200, headers={"cf-mitigated": "challenge"})):
        r.request = httpx.Request("GET", "https://a.com/shop")
        lim.record(r)
    assert lim.interval == 4.0 and not lim._answered
    ok = httpx.Response(200, request=httpx.Request("GET", "https://a.com/shop"))
    lim.record(ok)
    assert lim.interval < 4.0


# --- registry: vendor sites are not roasters -----------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["tryperdiem.com", "www.tryperdiem.com", "us.orderspoon.com", "orderspoon.com"]
)
def test_ordering_vendors_are_not_roaster_sites(host):
    """Owner's decision (review item 7): Per Diem's site was attached by Overture to Java House's North Liberty
    location, and Spoon's ordering marketplace to The Roasted Mullet. Neither is a roaster's store; discovery and
    add-roaster now refuse them, as they refuse other ordering platforms."""
    assert not registry.is_storefront_host(registry.normalize_domain(host))


def test_the_vendor_sites_are_gone_from_the_seed_and_java_house_keeps_its_own():
    seed = {r["roaster_id"]: r for r in registry.load_seed()}
    assert "tryperdiem.com" not in seed and "us.orderspoon.com" not in seed
    assert seed["thejavahouse.com"]["name"] == "Java House Coffee Roasters"


# --- registry corrections: Square Online stores that moved ---------------------------------------------------

# Each hand-checked on 2026-11-02 (PR body): DNS from the investigation, one probe of the home page (the existing
# detector, unchanged), and the store's catalog path. roaster_id -> (final_host, platform).
MOVED = {
    "kismetcoffeeroasters.com": ("www.kismetcoffeeroasters.com", "other"),  # Next.js on Vercel
    "publichouseri.com": ("publichouseri.com", "other"),  # Next.js on Vercel; a cafe menu
    "ridgeandroastcoffee.com": ("ridgeandroastcoffee.com", "other"),  # Netlify; a cafe menu
    "siliconvalley.coffee": ("www.siliconvalley.coffee", "webflow?"),  # Webflow
    "wydahoroaster.com": ("wydahoroaster.com", "other"),  # Railway
    "loneoakcoffee.com": ("loneoakcoffee.com", "shopify"),  # /products.json answers
    "nottinghillcoffee.com": ("nottinghillcoffee.com", "other"),  # GoDaddy Websites + Marketing
    "ponderosaroasting.com": ("ponderosaroasting.com", "other"),  # WordPress + WP EasyCart
    "publikcoffee.com": ("publikcoffee.com", "other"),  # custom site
    "recdejabrew.com": ("www.recdejabrew.com", "other"),  # IONOS MyWebsite; its square.site link is a 404
    "toomerscoffee.com": ("www.toomerscoffee.com", "other"),  # WordPress + WP EasyCart
}


@pytest.mark.parametrize("rid", sorted(MOVED))
def test_a_square_online_store_that_moved_has_its_new_platform_in_the_seed(rid):
    seed = {r["roaster_id"]: r for r in registry.load_seed()}
    host, platform = MOVED[rid]
    row = seed[rid]
    assert (row["final_host"], row["platform"], row["last_probed"]) == (host, platform, "2026-11-02")
    roaster = Roaster(roaster_id=rid, name=row["name"], host=host, platform=platform)
    assert roaster.adapter == ("shopify" if platform == "shopify" else "generic")


def test_the_corrections_reach_the_registry_state(lake):
    """registry.current reads the seed's platform when the lake has no probe for the roaster (none of these has)."""
    states = {s.roaster.roaster_id: s.roaster for s in registry.current(lake)}
    for rid, (host, platform) in MOVED.items():
        assert (states[rid].host, states[rid].platform) == (host, platform), rid
    # Not one of these: still Square Online, on a store subdomain (left for its own change: see the PR)
    assert states["littleriverroasting.com"].platform == "square_online?"
    assert date.fromisoformat(registry.load_seed()[0]["last_probed"])  # the seed still parses
