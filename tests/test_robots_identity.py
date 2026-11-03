"""RegistryCoffeeBot's robots.txt handling: which groups apply to us (RFC 9309 product tokens, the old `coffee-data`
token too), and robots.txt Crawl-delay, honoured up to 60 s (capped above it) and never past the deadline."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from coffeedata import http, ingest
from coffeedata.http import (
    MAX_CRAWL_DELAY,
    USER_AGENT,
    Blocked,
    DeadlineReached,
    PartialCatalog,
    PoliteClient,
    RequestStats,
    RobotsRules,
)
from coffeedata.models import Roaster

PAGE = "https://a.com/products.json"


@pytest.fixture
def sleeps(monkeypatch):
    """Every sleep the client asks for, on a fake clock that each sleep moves on (no real sleeping)."""
    out: list[float] = []
    now = [1000.0]

    def sleep(s: float) -> None:
        out.append(s)
        now[0] += s

    monkeypatch.setattr(http.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(http.time, "sleep", sleep)
    return out


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(PAGE).mock(return_value=httpx.Response(200, json={"products": []}))
        yield m


def _robots(mock: respx.MockRouter, text: str) -> None:
    mock.get("https://a.com/robots.txt").mock(return_value=httpx.Response(200, text=text))


def test_user_agent_names_the_bot_and_its_page():
    assert USER_AGENT == "RegistryCoffeeBot/1.0 (+https://registry.coffee/bot; support@registry.coffee)"


# --- which groups apply -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, allowed",
    [
        ("User-agent: *\nDisallow: /products.json\n", False),
        ("User-agent: RegistryCoffeeBot\nDisallow: /products.json\n", False),
        ("User-agent: registrycoffeebot\nDisallow: /products.json\n", False),  # case-insensitive
        ("User-agent: RegistryCoffeeBot/1.0\nDisallow: /products.json\n", False),  # a version is ignored
        ("User-agent: coffee-data\nDisallow: /products.json\n", False),  # the old token still applies
        ("User-agent: Coffee-Data\nDisallow: /\n", False),
        # A group naming us replaces `*`
        ("User-agent: *\nDisallow: /\n\nUser-agent: RegistryCoffeeBot\nAllow: /\n", True),
        # ...but a group naming only the old token doesn't: the token we send has none, so `*` applies to it too
        ("User-agent: *\nDisallow: /\n\nUser-agent: coffee-data\nAllow: /products.json\n", False),
        # Whole tokens, never substrings: neither "coffee" nor "Bot" names us
        ("User-agent: coffee\nDisallow: /\n", True),
        ("User-agent: Bot\nDisallow: /\n", True),
        ("User-agent: RegistryCoffeeBotX\nDisallow: /\n", True),
        # The exact product token: another crawler's name that starts with ours isn't us (and doesn't hide `*`)
        ("User-agent: RegistryCoffeeBot2\nDisallow: /\n", True),
        ("User-agent: coffee-data.v2\nDisallow: /\n", True),
        ("User-agent: RegistryCoffeeBot2\nAllow: /\n\nUser-agent: *\nDisallow: /\n", False),
        ("User-agent: RegistryCoffeeBot 1.0\nDisallow: /products.json\n", False),
        # Groups for both tokens: each one's rules apply
        (
            "User-agent: RegistryCoffeeBot\nAllow: /\n\nUser-agent: coffee-data\nDisallow: /products.json\n",
            False,
        ),
        (
            "User-agent: coffee-data\nAllow: /\n\nUser-agent: RegistryCoffeeBot\nDisallow: /products.json\n",
            False,
        ),
        ("User-agent: RegistryCoffeeBot\nAllow: /\n\nUser-agent: coffee-data\nAllow: /\n", True),
        # One group naming both, and a token's two groups combined
        ("User-agent: coffee-data\nUser-agent: RegistryCoffeeBot\nDisallow: /products.json\n", False),
        (
            "User-agent: RegistryCoffeeBot\nDisallow: /cart\n\nUser-agent: RegistryCoffeeBot\nDisallow: /products\n",
            False,
        ),
        ("User-agent: OtherBot\nDisallow: /\n", True),
        ("", True),
    ],
)
def test_groups_that_apply_to_us(text, allowed):
    assert RobotsRules(text.splitlines()).can_fetch(PAGE) is allowed


def test_a_group_naming_the_old_token_blocks_a_fetch(mock):
    _robots(mock, "User-agent: coffee-data\nDisallow: /products.json\n")
    with PoliteClient(min_interval=0) as c, pytest.raises(Blocked):
        c.get(PAGE)


def test_requests_send_the_new_user_agent(mock, sleeps):
    _robots(mock, "")
    with PoliteClient(min_interval=0) as c:
        c.get(PAGE)
    assert {call.request.headers["user-agent"] for call in mock.calls} == {USER_AGENT}


# --- Crawl-delay ----------------------------------------------------------------------------------------------


def _spacing(mock, sleeps, robots: str, **kw) -> tuple[float, list[dict]]:
    """The longest wait between two requests to a host with this robots.txt (robots.txt, then two pages: each
    page waits its turn after the request before it), and the fetch's crawl_delays."""
    _robots(mock, robots)
    with PoliteClient(min_interval=1.0, retries=1, **kw) as c, c.track() as stats:
        c.get(PAGE)
        c.get(PAGE)
    return max(sleeps, default=0.0), stats.crawl_delays


def test_no_crawl_delay_keeps_our_spacing(mock, sleeps):
    wait, delays = _spacing(mock, sleeps, "User-agent: *\nAllow: /\n")
    assert 0.9 < wait <= 1.0 and delays == []


@pytest.mark.parametrize(
    "asked, spacing, capped",
    [("10", 10.0, False), ("60", 60.0, False), ("61", 60.0, True), ("3600", 60.0, True),
     ("2.5", 2.5, False), ("0.5", 1.0, False)],
)  # fmt: skip
def test_crawl_delay_up_to_sixty_seconds_then_capped(mock, sleeps, asked, spacing, capped):
    wait, delays = _spacing(mock, sleeps, f"User-agent: *\nCrawl-delay: {asked}\n")
    assert spacing - 0.1 < wait <= spacing
    assert delays == [{"host": "a.com", "asked": asked, "applied_s": spacing, "capped": capped}]
    assert MAX_CRAWL_DELAY == 60.0


@pytest.mark.parametrize(
    "asked, spacing, capped",
    [("10s", 10.0, False), (".5", 1.0, False), ("5.", 5.0, False), ("1e3", 60.0, True), ("2 seconds", 2.0, False),
     ("1e999", 60.0, True)],
)  # fmt: skip
def test_a_malformed_crawl_delay_reads_its_leading_number(mock, sleeps, asked, spacing, capped):
    wait, delays = _spacing(mock, sleeps, f"User-agent: *\nCrawl-delay: {asked}\n")
    assert spacing - 0.1 < wait <= spacing
    assert delays == [{"host": "a.com", "asked": asked, "applied_s": spacing, "capped": capped}]


@pytest.mark.parametrize("asked", ["ten", "-5", "", "fast"])
def test_a_crawl_delay_with_no_number_gets_the_cap_never_ignored(mock, sleeps, asked):
    """The owner's rule: a Crawl-delay is never ignored. One we can't read is waited at the cap (60 s)."""
    wait, delays = _spacing(mock, sleeps, f"User-agent: *\nCrawl-delay: {asked}\n")
    assert MAX_CRAWL_DELAY - 0.1 < wait <= MAX_CRAWL_DELAY
    assert delays == [{"host": "a.com", "asked": asked, "applied_s": MAX_CRAWL_DELAY, "capped": True}]


@pytest.mark.parametrize(
    "robots, spacing",
    [
        (
            "User-agent: *\nCrawl-delay: 20\n\nUser-agent: RegistryCoffeeBot\nCrawl-delay: 5\n",
            5.0,
        ),  # ours, not *
        (
            "User-agent: *\nCrawl-delay: 20\n\nUser-agent: RegistryCoffeeBot\nAllow: /\n",
            1.0,
        ),  # ours asks none
        ("User-agent: OtherBot\nCrawl-delay: 20\n\nUser-agent: *\nCrawl-delay: 7\n", 7.0),  # * applies
        ("User-agent: coffee-data\nCrawl-delay: 30\n", 30.0),  # the old token's
        (
            "User-agent: coffee-data\nCrawl-delay: 3\n\nUser-agent: *\nCrawl-delay: 9\n",
            9.0,
        ),  # and `*`, for ours
        ("User-agent: RegistryCoffeeBot\nCrawl-delay: 5\n\nUser-agent: coffee-data\nCrawl-delay: 30\n", 30.0),
    ],
)
def test_crawl_delay_from_our_group_or_star(mock, sleeps, robots, spacing):
    wait, _ = _spacing(mock, sleeps, robots)
    assert spacing - 0.1 < wait <= spacing


def test_a_crawl_delay_is_never_waited_out_past_the_deadline(mock, sleeps):
    """A 60 s Crawl-delay with 90 s of the run left: robots.txt, the first page 60 s later, and the second page's
    turn falls after the deadline, so the fetch stops at once (DeadlineReached) instead of holding the worker."""
    _robots(mock, "User-agent: *\nCrawl-delay: 60\n")
    with PoliteClient(min_interval=1.0, retries=1, deadline_s=90) as c:
        c.get(PAGE)
        with pytest.raises(DeadlineReached):
            c.get(PAGE)
    assert sleeps == [60.0]


def test_a_fetch_stopped_at_the_deadline_still_records_its_crawl_delay(mock, sleeps):
    """crawl_delays explains exactly this row: the fetch DeadlineReached stopped."""
    _robots(mock, "User-agent: *\nCrawl-delay: 60\n")
    # the first page's turn is 60 s after robots.txt: past the deadline
    with (
        PoliteClient(min_interval=1.0, retries=1, deadline_s=30) as c,
        c.track() as stats,
        pytest.raises(DeadlineReached),
    ):
        c.get(PAGE)
    assert stats.crawl_delays == [{"host": "a.com", "asked": "60", "applied_s": 60.0, "capped": False}]


def test_the_deadline_stops_a_store_without_a_crawl_delay_too(mock, sleeps):
    """#99's check after the wait: with no Crawl-delay, no request is sent once the deadline has passed either."""
    _robots(mock, "")
    with PoliteClient(min_interval=1.0, retries=1, deadline_s=0) as c, pytest.raises(DeadlineReached):
        c.get(PAGE)
    assert not mock.routes[0].called


def test_ingest_passes_the_deadline_to_the_client(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_client(**kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(ingest, "PoliteClient", fake_client)
    monkeypatch.setattr(ingest, "_known_bodies", lambda lake: set())
    monkeypatch.setattr(ingest, "known_products", lambda lake: {})
    monkeypatch.setattr(ingest, "_done_and_tried", lambda lake, rd: (set(), set()))
    monkeypatch.setattr(ingest, "blocked_hosts", lambda lake, rd: set())

    class Lake:
        def write_partition(self, *a, **k):
            return None

        def publish(self, *a, **k):
            return None

    with pytest.raises(Stop):
        ingest.run(Lake(), [], "2026-11-02", deadline_s=123.0)  # type: ignore[arg-type]
    # the run's deadline, counted from its start: what's left of it, never a fresh 123 s from the client's creation
    assert 0 < seen["deadline_s"] < 123.0


def test_the_client_deadline_is_the_runs_not_a_later_one(monkeypatch):
    """The manifest and lake scans before the client is made took 100 s: the client gets the 23 s left."""
    seen = {}
    now = [1000.0]

    class Stop(Exception):
        pass

    def fake_client(**kw):
        seen.update(kw)
        raise Stop

    def slow_known_bodies(lake):
        now[0] += 100.0
        return set()

    monkeypatch.setattr(ingest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ingest, "PoliteClient", fake_client)
    monkeypatch.setattr(ingest, "_known_bodies", slow_known_bodies)
    monkeypatch.setattr(ingest, "known_products", lambda lake: {})
    monkeypatch.setattr(ingest, "_done_and_tried", lambda lake, rd: (set(), set()))
    monkeypatch.setattr(ingest, "blocked_hosts", lambda lake, rd: set())

    class Lake:
        def write_partition(self, *a, **k):
            return None

        def publish(self, *a, **k):
            return None

    with pytest.raises(Stop):
        ingest.run(Lake(), [], "2026-11-02", deadline_s=123.0)  # type: ignore[arg-type]
    assert seen["deadline_s"] == pytest.approx(23.0)


def test_the_fetches_row_records_the_crawl_delay():
    stats = RequestStats()
    record = {"host": "a.com", "asked": "3600", "applied_s": 60.0, "capped": True}
    stats.note_delay(record)
    stats.note_delay(record)  # once per host
    roaster = Roaster(roaster_id="a.com", name="A", host="a.com", platform="shopify")
    res = ingest.Result(roaster, datetime(2026, 11, 2, tzinfo=UTC), stats=stats)
    row = ingest.rows_for([res], date(2026, 11, 2), "b1", set())["fetches"][0]
    assert row["crawl_delays"] == [record]
    res2 = ingest.Result(roaster, datetime(2026, 11, 2, tzinfo=UTC))
    assert ingest.rows_for([res2], date(2026, 11, 2), "b1", set())["fetches"][0]["crawl_delays"] is None


# --- PR #100's review: the old token's group never hides the `*` group from the token we send -------------------


def test_review_case_1_a_coffee_data_group_doesnt_lift_the_star_groups_disallow():
    """RFC 9309 2.2.1: with no RegistryCoffeeBot group, `*` applies to the token we send, whatever coffee-data's
    group says. Before, only the coffee-data group applied and /w/p was fetched."""
    rules = RobotsRules(
        "User-agent: coffee-data\nDisallow: /cart\n\nUser-agent: *\nDisallow: /w\n".splitlines()
    )
    assert rules.can_fetch("https://x/w/p") is False
    assert rules.can_fetch("https://x/cart") is False  # coffee-data's own rule still applies
    assert rules.can_fetch("https://x/products.json") is True


def test_review_case_2_a_coffee_data_allow_doesnt_override_a_star_disallow_all():
    rules = RobotsRules("User-agent: coffee-data\nAllow: /\n\nUser-agent: *\nDisallow: /\n".splitlines())
    assert rules.can_fetch("https://x/w/p") is False


def test_a_registrycoffeebot_group_replaces_star_and_coffee_data_still_applies():
    text = (
        "User-agent: *\nDisallow: /\n\nUser-agent: RegistryCoffeeBot\nAllow: /\n\n"
        "User-agent: coffee-data\nDisallow: /private\n"
    )
    rules = RobotsRules(text.splitlines())
    assert rules.can_fetch("https://x/products.json") is True  # `*` doesn't apply: a group names us
    assert rules.can_fetch("https://x/private/a") is False  # the old token's group still does


# --- RFC 9309 2.2.2 (longest match) and 2.2.3 (wildcards), evaluated by RobotsRules itself ----------------------


@pytest.mark.parametrize(
    "rules, path, allowed",
    [
        # 2.2.2: the most specific (longest) matching rule wins, whatever the order it is written in
        ("Disallow: /\nAllow: /products.json", "/products.json", True),
        ("Allow: /products.json\nDisallow: /", "/products.json", True),
        ("Allow: /p\nDisallow: /products", "/products.json", False),
        ("Disallow: /products\nAllow: /p", "/products.json", False),
        ("Disallow: /products\nAllow: /p", "/pages", True),
        # 2.2.2: an allow and a disallow equally long: the least restrictive, allow
        ("Disallow: /page\nAllow: /page", "/page", True),
        ("Allow: /page\nDisallow: /page", "/page", True),
        # 2.2.2: no matching rule, or an empty Disallow, allows; /robots.txt is always allowed
        ("Disallow: /cart", "/products.json", True),
        ("Disallow:", "/anything", True),
        ("Disallow: /", "/robots.txt", True),
        # 2.2.2: rules match the path and its query
        ("Disallow: /search?q=", "/search?q=beans", False),
        ("Disallow: /search?q=", "/search", True),
        # 2.2.2: compared after percent-encoding normalization
        ("Disallow: /caf%C3%A9", "/café", False),
        ("Disallow: /a%2fb", "/a%2Fb", False),
        # 2.2.3: `*` matches any run of characters (none too)
        ("Disallow: /*/products.json", "/collections/all/products.json", False),
        ("Disallow: /*.json", "/products.json", False),
        ("Disallow: /*.json", "/products.xml", True),
        ("Disallow: /*?*variant=", "/products/a?variant=1", False),
        ("Disallow: /*?*variant=", "/products/a", True),
        ("Disallow: /p*s", "/ps", False),
        # 2.2.3: a final `$` anchors the end of the path; without it a rule is a prefix
        ("Disallow: /*.json$", "/products.json", False),
        ("Disallow: /*.json$", "/products.json?page=2", True),
        ("Disallow: /products$", "/products", False),
        ("Disallow: /products$", "/products/a", True),
        ("Disallow: /products", "/products/a", False),
        # 2.2.2 with 2.2.3: specificity is the pattern's length, wildcards included
        ("Disallow: /*.json\nAllow: /products.json", "/products.json", True),
        ("Allow: /*\nDisallow: /", "/x", True),
        # Shopify's default robots.txt (2026): checkout and filtered collections disallowed, the feed allowed
        (
            "Disallow: /checkout\nDisallow: /collections/*+*\nDisallow: /*/collections/*sort_by*",
            "/products.json",
            True,
        ),
        ("Disallow: /checkout\nDisallow: /collections/*+*", "/collections/a+b", False),
        ("Disallow: /*/collections/*sort_by*", "/en/collections/all?sort_by=price", False),
        # PR #100's re-review: a `;` in the path is part of it (urlsplit; urlparse moved `;params` out of .path)
        ("Disallow: /a;b", "/a;b", False),
        ("Disallow: /*;jsessionid", "/p;jsessionid=1", False),
        # the re-review: a percent-encoded `*` or `$` is a literal, never wildcard syntax
        ("Disallow: /f/%2A.json", "/f/x.json", True),
        ("Disallow: /f/%2A.json", "/f/*.json", False),
        ("Disallow: /p%24", "/p%24x", False),
        ("Disallow: /p%24", "/p", True),
        # the linear matcher: a `$` before the end is a literal; an anchored pattern's last run must end the path
        ("Disallow: /a$b", "/a", True),
        ("Disallow: /a$b", "/a$bc", False),
        ("Disallow: /a*$", "/a/anything", False),
        ("Disallow: /*a*b$", "/xaybzb", False),
        ("Disallow: /*a*b$", "/xaybzbc", True),
        ("Disallow: /*ab*ab$", "/ab", True),  # the two runs can't overlap
        ("Disallow: /*ab*ab$", "/abab", False),
        ("Disallow: /$", "/", False),
        ("Disallow: /$", "/a", True),
    ],
)
def test_rfc9309_longest_match_and_wildcards(rules, path, allowed):
    text = "User-agent: RegistryCoffeeBot\n" + rules + "\n"
    assert RobotsRules(text.splitlines()).can_fetch(f"https://x.com{path}") is allowed


def test_a_utf8_byte_order_mark_doesnt_hide_the_first_group():
    rules = RobotsRules("\ufeffUser-agent: *\nDisallow: /\n".splitlines())
    assert rules.can_fetch("https://x.com/a") is False


def test_a_hostile_wildcard_pattern_is_matched_in_linear_time():
    """The re-review's case: `/*a*a*a*a*a*a*a*X` took 0.34 s per check on a 40-character path as a backtracking
    regex. can_fetch runs before every request, so a hostile robots.txt must never stall a worker."""
    import time

    rules = RobotsRules(("User-agent: *\nDisallow: /" + "*a" * 30 + "*X\n").splitlines())
    url = "https://x.com/" + "a" * 2000
    start = time.perf_counter()
    for _ in range(100):
        assert rules.can_fetch(url) is True
    assert time.perf_counter() - start < 0.5


def test_robots_txt_is_spaced_like_any_request(mock, sleeps):
    """The /bot page's promise, at least 1 s between requests to a store: robots.txt counts, so the first catalog
    request waits its turn after it."""
    _robots(mock, "")
    with PoliteClient(min_interval=1.0, retries=1) as c:
        c.get(PAGE)
    assert sleeps == [1.0]


# --- a deadline stop is never an ok fetch with null values -------------------------------------------------------

WOO = "https://w.com/wp-json/wc/store/v1/products"


def _woo_mock(m: respx.MockRouter) -> None:
    product = {
        "id": 7,
        "name": "Blend",
        "permalink": "https://w.com/p/blend",
        "type": "variable",
        "variations": [
            {"id": 8, "attributes": [{"name": "Size", "value": "12 oz"}]},
            {"id": 9, "attributes": [{"name": "Size", "value": "2 lb"}]},
        ],
        "prices": {
            "price": "1800",
            "currency_minor_unit": 2,
            "price_range": {"min_amount": "1800", "max_amount": "4000"},
        },
    }
    m.get("https://w.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")
    )
    m.get(WOO).mock(return_value=httpx.Response(200, json=[product]))
    for vid, price in ((8, "1800"), (9, "4000")):
        m.get(f"{WOO}/{vid}").mock(
            return_value=httpx.Response(
                200, json={"id": vid, "prices": {"price": price, "currency_minor_unit": 2}}
            )
        )


def test_a_woocommerce_variation_lookup_stopped_at_the_deadline_fails_the_fetch(sleeps):
    """The review's reproduction: Crawl-delay 30, with time for robots.txt and the listing but not for a variation
    (45 s: robots.txt, the listing at 30 s, the first variation's turn at 60 s). Before, both variations came back with a None price
    and the fetch was an ok row; now the deadline stop fails the fetch, so last week's prices stand."""
    from coffeedata.adapters import woocommerce

    roaster = Roaster(roaster_id="w.com", name="W", host="w.com", platform="woocommerce")
    with respx.mock(assert_all_called=False) as m:
        _woo_mock(m)
        with (
            PoliteClient(min_interval=1.0, retries=1, deadline_s=45) as c,
            pytest.raises(PartialCatalog) as stop,
        ):
            woocommerce.fetch(c, roaster)
        assert stop.value.items == [] and isinstance(stop.value.__cause__, DeadlineReached)
        assert [c.request.url.path for c in m.calls] == ["/robots.txt", "/wp-json/wc/store/v1/products"]
        with PoliteClient(min_interval=1.0, retries=1, deadline_s=45) as c:
            res = ingest.ingest_one(c, roaster)
    assert res.status == "error" and res.items == [] and "DeadlineReached" in (res.error or "")
    assert res.stats.crawl_delays == [{"host": "w.com", "asked": "30", "applied_s": 30.0, "capped": False}]


def test_a_squarespace_sitemap_stopped_at_the_deadline_is_the_deadline_not_unreadable(sleeps, caplog):
    from coffeedata.adapters import squarespace

    roaster = Roaster(roaster_id="sq.com", name="S", host="sq.com", platform="squarespace")
    with respx.mock(assert_all_called=False) as m:
        m.get("https://sq.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 60\n")
        )
        sitemap = m.get("https://sq.com/sitemap.xml").mock(return_value=httpx.Response(404))
        # robots.txt is the host's first request: the sitemap's turn is 60 s on, past the deadline
        with (
            PoliteClient(min_interval=1.0, retries=1, deadline_s=30) as c,
            pytest.raises(PartialCatalog) as stop,
        ):
            squarespace.fetch(c, roaster)
    assert stop.value.items == [] and isinstance(stop.value.__cause__, DeadlineReached)
    assert sitemap.call_count == 0
    assert "sitemap unreadable" not in caplog.text


def test_the_platform_probe_is_spaced_like_the_crawl_and_honours_robots(monkeypatch):
    """/bot promises at least 1 s between requests to a store and robots.txt first: the Monday probe too."""
    from coffeedata import discover

    made = {}
    real = discover.PoliteClient

    def client(**kw):
        made.update(kw)
        return real(**kw)

    monkeypatch.setattr(discover, "PoliteClient", client)
    with respx.mock(assert_all_called=False) as m:
        m.get(url__regex=r"https://(www\.)?a\.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: RegistryCoffeeBot\nDisallow: /\n")
        )
        home = m.get(url__regex=r"https://(www\.)?a\.com/$").mock(return_value=httpx.Response(200))
        rows = discover.probe(["a.com"], workers=1, run_date="2026-11-02")
    assert made.get("min_interval", 1.0) >= 1.0
    assert rows[0]["status"] == "blocked" and home.call_count == 0
