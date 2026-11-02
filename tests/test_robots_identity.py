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
    PoliteClient,
    RequestStats,
    RobotsRules,
)
from coffeedata.models import Roaster

PAGE = "https://a.com/products.json"


@pytest.fixture
def sleeps(monkeypatch):
    """Every sleep the client asks for, without sleeping."""
    out: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: out.append(s))
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
        ("User-agent: *\nDisallow: /\n\nUser-agent: coffee-data\nAllow: /products.json\n", True),
        # Whole tokens, never substrings: neither "coffee" nor "Bot" names us
        ("User-agent: coffee\nDisallow: /\n", True),
        ("User-agent: Bot\nDisallow: /\n", True),
        ("User-agent: RegistryCoffeeBotX\nDisallow: /\n", True),
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
    """The longest wait between two requests to a host with this robots.txt, and the fetch's crawl_delays."""
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
def test_crawl_delay_up_to_five_minutes(mock, sleeps, asked, spacing, capped):
    wait, delays = _spacing(mock, sleeps, f"User-agent: *\nCrawl-delay: {asked}\n")
    assert spacing - 0.1 < wait <= spacing
    assert delays == [{"host": "a.com", "asked": asked, "applied_s": spacing, "capped": capped}]
    assert MAX_CRAWL_DELAY == 60.0


@pytest.mark.parametrize("asked", ["ten", "-5", "", "1e3", "10s"])
def test_a_malformed_crawl_delay_keeps_our_spacing(mock, sleeps, asked):
    wait, delays = _spacing(mock, sleeps, f"User-agent: *\nCrawl-delay: {asked}\n")
    assert 0.9 < wait <= 1.0
    assert delays == [{"host": "a.com", "asked": asked, "applied_s": 1.0, "capped": False}]


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
        ("User-agent: RegistryCoffeeBot\nCrawl-delay: 5\n\nUser-agent: coffee-data\nCrawl-delay: 30\n", 30.0),
    ],
)
def test_crawl_delay_from_our_group_or_star(mock, sleeps, robots, spacing):
    wait, _ = _spacing(mock, sleeps, robots)
    assert spacing - 0.1 < wait <= spacing


def test_a_crawl_delay_is_never_waited_out_past_the_deadline(mock, sleeps):
    """A 60 s Crawl-delay with 30 s of the run left: the second request's turn falls after the deadline, so the
    fetch stops at once (DeadlineReached) instead of holding the worker past it."""
    _robots(mock, "User-agent: *\nCrawl-delay: 60\n")
    with PoliteClient(min_interval=1.0, retries=1, deadline_s=30) as c:
        c.get(PAGE)
        with pytest.raises(DeadlineReached):
            c.get(PAGE)
    assert max(sleeps, default=0.0) < 1.0


def test_the_deadline_leaves_our_own_spacing_alone(mock, sleeps):
    """Without a Crawl-delay the deadline doesn't stop a fetch in the client (ingest.run stops starting new ones)."""
    _robots(mock, "")
    with PoliteClient(min_interval=1.0, retries=1, deadline_s=0) as c:
        c.get(PAGE)
        c.get(PAGE)


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
    assert seen["deadline_s"] == 123.0


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
