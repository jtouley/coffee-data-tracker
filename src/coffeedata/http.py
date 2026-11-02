"""A polite HTTP client: identifies itself, honors robots.txt, spaces requests per host, retries."""

from __future__ import annotations

import contextlib
import copy
import email.utils
import logging
import os
import re
import threading
import time
import urllib.robotparser
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import httpx

from .text import scrub_surrogates

if TYPE_CHECKING:
    from .ratelimit import PlatformLimiter

log = logging.getLogger(__name__)

USER_AGENT = "RegistryCoffeeBot/1.0 (+https://registry.coffee/bot; support@registry.coffee)"
# Our robots.txt product tokens (RFC 9309): the current one, and the one we crawled under before (`coffee-data`), so a
# store's rules for either keep applying. A group applies when its user-agent line names one of them, matched
# case-insensitively as a whole token (never as a substring); only when no group names us does the `*` group apply.
ROBOTS_TOKENS = ("RegistryCoffeeBot", "coffee-data")
# The longest robots.txt Crawl-delay we wait between requests to one host (60 s), so a catalog still finishes within
# the crawl window. A longer one is capped here, and the cap is recorded (fetches.crawl_delays); a store that wants
# slower crawling can email us. Separate from the cap on one-off waits a store asks for (Retry-After).
MAX_CRAWL_DELAY = 60.0
# "Slow down": 429, Shopify's 430 (an IP-wide security block), and a 503 that can mean the same.
BLOCK_STATUSES = (429, 430, 503)
RETRY_STATUSES = (429, 430, 500, 502, 503, 504)
# A robots.txt that didn't answer is remembered this long, so the host's fetches fail fast instead of each asking
# again. A site whose robots.txt always answers 5xx is never crawled: by design (RFC 9309 allows a 5xx to mean
# "disallow everything", and Google treats it so); we never fetch without a store's rules.
ROBOTS_RETRY_AFTER = 900.0
# A robots.txt that answered a block (429, 430, 503) is a pause, not a non-answer: it is remembered for its
# Retry-After, else this long, and the store is deferred (ingest.run), not given up on.
ROBOTS_BLOCK_RETRY_AFTER = 60.0
# The longest the client waits between its own tries of one URL. A Retry-After longer than this isn't cut short: the
# client stops trying, and the store is deferred until it has passed (ingest.run).
MAX_RETRY_WAIT = 30.0
MAX_REDIRECTS = 20
# The most body one response may bring into memory, decompressed (COFFEEDATA_MAX_RESPONSE_MB, default 32 MiB). A
# catalog page or a product page is far below it; a video or a runaway page at a product URL is not, and fails that
# roaster's fetch (ResponseTooLarge) instead of the job's memory (crawl-generic's OOM kill, 2026-11-02). Sitemaps get
# their own, higher cap (sitemap.SITEMAP_MAX_BYTES, through with_max_response): the protocol allows 50 MB.
MAX_RESPONSE_BYTES = int(float(os.environ.get("COFFEEDATA_MAX_RESPONSE_MB") or 32) * 2**20)


# An edge's challenge in place of the page: SiteGround's anti-bot answers HTTP 202 with `sg-captcha: challenge` (and
# a body that sends a browser to /.well-known/sgcaptcha/), from the first request on and to every retry (run
# 2026-11-02: 38 stores, all on SiteGround, challenged from Cloud Run's shared egress). A refusal, not a pause: never
# retried, never deferred within the run, never read as content, and never as a store's robots.txt.
CHALLENGE_STATUSES = (202,)
# Headers an edge sets only on a challenge (any value). Cloudflare's is `cf-mitigated: challenge` (is_challenge).
CHALLENGE_HEADERS = ("sg-captcha",)
# A refusal or block. Each one is recorded with the headers Shopify Support asks for (and that tell a Cloudflare
# challenge, `cf-mitigated: challenge` with an HTML body, or SiteGround's, `sg-captcha`, from a rate limit), plus
# the egress IP it was sent from.
LOGGED_BLOCK_STATUSES = (*CHALLENGE_STATUSES, 403, 429, 430, 503)
BLOCK_HEADERS = (
    "server",
    "cf-mitigated",
    "content-type",
    "retry-after",
    "cf-ray",
    "x-request-id",
    "sg-captcha",
)
MAX_BLOCKS_PER_FETCH = 20  # a store that refuses every try doesn't fill its fetch row with copies
EGRESS_TTL = 60.0  # seconds an egress-IP lookup is reused (default egress can change IP between connections)
_egress_lock = threading.Lock()
_egress: dict[str, Any] = {"ip": None, "at": None}


def egress_ip() -> str | None:
    """The public IP this process's requests leave from, as best known: COFFEEDATA_EGRESS_IP if the job sets it
    (a dedicated NAT IP is known in advance), else a lookup at COFFEEDATA_EGRESS_ECHO_URL (a plain-text "what is
    my IP" endpoint, not a store), reused for EGRESS_TTL; None when neither is configured or the lookup fails."""
    if fixed := os.environ.get("COFFEEDATA_EGRESS_IP"):
        return fixed
    url = os.environ.get("COFFEEDATA_EGRESS_ECHO_URL")
    if not url:
        return None
    with _egress_lock:
        if _egress["at"] is not None and time.monotonic() - _egress["at"] < EGRESS_TTL:
            return _egress["ip"]
        try:
            r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=5.0)
            text = r.text.strip()
            ip = text if r.status_code == 200 and 0 < len(text) <= 45 and " " not in text else None
        except httpx.HTTPError:
            ip = None
        _egress.update(ip=ip, at=time.monotonic())
        return ip


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After as seconds: delta-seconds or an HTTP date. None if missing or unreadable."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def block_status(e: BaseException | None) -> int | None:
    """The block status (BLOCK_STATUSES) a failure carries, itself or in its causes: a FetchError's status, or the
    response of a wrapped HTTPStatusError. None for anything else."""
    while e is not None:
        if isinstance(e, FetchError) and e.status in BLOCK_STATUSES:
            return e.status
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in BLOCK_STATUSES:
            return e.response.status_code
        e = e.__cause__
    return None


def is_challenge(r: httpx.Response) -> bool:
    """The response is an edge's challenge, not the page: a CHALLENGE_STATUSES answer, a SiteGround `sg-captcha`
    header, or Cloudflare's `cf-mitigated: challenge`, whatever the status."""
    h = r.headers
    return (
        r.status_code in CHALLENGE_STATUSES
        or any(k in h for k in CHALLENGE_HEADERS)
        or h.get("cf-mitigated", "").lower() == "challenge"
    )


def block_record(r: httpx.Response) -> dict:
    """What one refused or blocked response said about itself (raw.fetches.blocks)."""
    h = r.headers
    return {"at": datetime.now(UTC).replace(microsecond=0), "status": r.status_code, "host": r.url.host,
            "path": r.url.path, **{k.replace("-", "_"): h.get(k) for k in BLOCK_HEADERS},
            "egress_ip": egress_ip()}  # fmt: skip


class FetchError(Exception):
    """A catalog request failed (non-200, bad JSON, blocked). The roaster's fetch is recorded as an error. status:
    the HTTP status, when the failure was a non-200 answer (ingest.blocked reads it)."""

    def __init__(self, *args: object, status: int | None = None) -> None:
        super().__init__(*args)
        self.status = status


class ResponseTooLarge(FetchError):
    """A response body over the client's max_response_bytes (said by its content-length, or counted as it was read,
    decompressed). Not an httpx.HTTPError: it is never retried, and it fails the roaster's fetch. status: the
    response's HTTP status."""


class Blocked(FetchError):
    """robots.txt disallows the URL."""


class Unavailable(FetchError):
    """The store is closed, deactivated or password-protected: Shopify's "This store is unavailable" page, a redirect
    to /password, or a robots.txt that disallows the whole site. A fetch status of its own (`unavailable`), not an
    error or a block: retrying won't help, and it doesn't count against the run's ok rate."""


class SiteDisallowed(Unavailable, Blocked):
    """robots.txt disallows the whole site (/), not just the catalog path: the store is closed to us (Big Guns,
    deactivated, 2026-10-31). Still a Blocked, so callers that try another path on a block keep doing so."""


# What Shopify's closed or deactivated storefront page says (lowercased). Read only on a non-200, non-JSON answer
# to Shopify's catalog path (SHOPIFY_CATALOG_PATH): another platform's page with the same words (a Squarespace
# shop "currently unavailable until Monday") is not this signature, and neither is any 200 page.
SHOPIFY_CATALOG_PATH = "/products.json"
UNAVAILABLE_MARKERS = (
    "this store is unavailable",
    "store is currently unavailable",
    "this shop is unavailable",
    "shop is currently unavailable",
)


def unavailable_reason(r: httpx.Response) -> str | None:
    """Why this response says the store is closed, if it does: a redirect to a password page, or Shopify's
    closed-store page (a non-200 HTML answer to /products.json with its wording)."""
    if r.url.path.rstrip("/") == "/password" or any(h.url.path.rstrip("/") == "/password" for h in r.history):
        return f"password-protected: {r.url.host}"
    closed_page = (
        r.status_code != 200
        and "json" not in r.headers.get("content-type", "")
        and (r.history[0] if r.history else r).request.url.path.rstrip("/") == SHOPIFY_CATALOG_PATH
    )
    if closed_page:
        text = r.text[:20_000].lower() if r.content else ""
        if any(m in text for m in UNAVAILABLE_MARKERS):
            return f"store unavailable (HTTP {r.status_code}): {r.url.host}"
    return None


class RobotsUnavailable(FetchError, httpx.HTTPError):
    """robots.txt didn't answer with the store's rules (429, 430, 5xx, an edge's challenge, a 2xx other than 200, a
    1xx or 3xx, or a network error) after the client's retries, so nothing on that host is fetched for
    ROBOTS_RETRY_AFTER (a block: for its Retry-After, else ROBOTS_BLOCK_RETRY_AFTER). Callers treat it like a
    network error: this fetch fails and a later one asks again. status: a block's status (ingest defers the store,
    after wait seconds) or a challenge's.
    unreachable: the host itself couldn't be reached (DNS, refused connection), not just robots.txt. challenge:
    robots.txt was answered with an edge's challenge (is_challenge: a 202, or a challenge header on any status),
    which ingest counts as a refusal and never defers."""

    def __init__(
        self,
        message: str,
        unreachable: bool = False,
        status: int | None = None,
        challenge: bool = False,
        wait: float | None = None,
    ) -> None:
        super().__init__(message, status=status)
        self.unreachable = unreachable
        self.challenge = challenge
        self.wait = wait


class DeadlineReached(FetchError):
    """The run's deadline (PoliteClient.deadline) would pass before this request's turn: the fetch stops where it
    is, instead of a long Crawl-delay holding a worker past the deadline."""


class NotCatalog(FetchError):
    """HTTP 200, but the body isn't the JSON a catalog endpoint returns (e.g. an HTML or challenge page)."""


@dataclass
class _RobotsGroup:
    agents: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)  # its allow and disallow lines, as written
    delays: list[str] = field(default_factory=list)  # its Crawl-delay values, as written


def _robots_groups(lines: Iterable[str]) -> list[_RobotsGroup]:
    """robots.txt's groups, as urllib.robotparser reads them: consecutive user-agent lines start a group, and a
    user-agent line after a rule line starts the next one. Lines before the first user-agent line are no group's."""
    groups: list[_RobotsGroup] = []
    group, in_rules = None, False
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if group is None or in_rules:
                group, in_rules = _RobotsGroup(), False
                groups.append(group)
            group.agents.append(value)
        elif key in ("allow", "disallow", "crawl-delay", "request-rate") and group is not None:
            in_rules = True
            if key == "crawl-delay":
                group.delays.append(value)
            elif key != "request-rate":
                group.rules.append(f"{key}: {value}")
    return groups


def _names(agent: str, token: str) -> bool:
    """A user-agent line names this product token (RFC 9309: case-insensitive; a version after it is ignored)."""
    m = re.match(r"[A-Za-z_-]+", agent.strip())
    return m is not None and m.group().lower() == token.lower()


def _delay_seconds(value: str) -> float | None:
    """A Crawl-delay value in seconds, or None when it isn't a plain non-negative number."""
    return float(value) if re.fullmatch(r"\d+(\.\d+)?", value) else None


class RobotsRules:
    """A host's robots.txt, for us. The groups naming one of our ROBOTS_TOKENS apply (a token's several groups are
    combined, RFC 9309); when groups name both tokens, a URL is fetched only if each token's rules allow it. Only
    when no group names us does the `*` group apply. The rules themselves are evaluated by urllib.robotparser.

    crawl_delay: the Crawl-delay those groups ask (the largest that is a number), or None; crawl_delay_asked: the
    value as written (a malformed one too)."""

    def __init__(self, lines: Iterable[str] = (), disallow_all: bool = False) -> None:
        self.disallow_all = disallow_all
        groups = _robots_groups(lines)
        ours = [[g for g in groups if any(_names(a, t) for a in g.agents)] for t in ROBOTS_TOKENS]
        selected = [gs for gs in ours if gs] or [[g for g in groups if any(a == "*" for a in g.agents)]]
        self._parsers: list[urllib.robotparser.RobotFileParser] = []
        asked: list[str] = []
        for gs in selected:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(["User-agent: *", *(rule for g in gs for rule in g.rules)])
            self._parsers.append(parser)
            asked += [d for g in gs for d in g.delays]
        numbers = [(n, d) for d in asked if (n := _delay_seconds(d)) is not None]
        self.crawl_delay: float | None = max(numbers)[0] if numbers else None
        self.crawl_delay_asked: str | None = max(numbers)[1] if numbers else (asked[0] if asked else None)

    def can_fetch(self, url: str) -> bool:
        if self.disallow_all:
            return False
        return all(p.can_fetch("*", url) for p in self._parsers)


@dataclass
class RequestStats:
    """What one tracked unit of work (a roaster's fetch) asked of the network: kept per thread by `track()`."""

    requests: int = 0  # every HTTP attempt, retries and robots.txt included
    pages: int = 0  # 200 responses other than robots.txt: listing pages, and any per-product or sitemap fetch
    retries: int = 0  # attempts after the first for one URL
    rate_limited: int = 0  # 429 responses
    statuses: Counter = field(default_factory=Counter)  # HTTP status -> responses; a network error has none
    # block_record() of each 202/403/429/430/503 or other challenge (is_challenge), the first 20
    blocks: list[dict] = field(default_factory=list)
    # Each host asked of whose robots.txt names a Crawl-delay for us: {host, asked (as written), applied_s (the
    # spacing used between its requests), capped (asked more than MAX_CRAWL_DELAY)}
    crawl_delays: list[dict] = field(default_factory=list)

    def note(
        self,
        status: int | None,
        retry: bool = False,
        page: bool = True,
        response: httpx.Response | None = None,
    ) -> None:
        if (
            response is not None
            and (status in LOGGED_BLOCK_STATUSES or is_challenge(response))
            and len(self.blocks) < MAX_BLOCKS_PER_FETCH
        ):
            self.blocks.append(block_record(response))
        self.requests += 1
        self.retries += retry
        if status is not None:
            self.statuses[status] += 1
            self.rate_limited += status == 429
            self.pages += page and status == 200

    def note_delay(self, record: dict) -> None:
        if all(d["host"] != record["host"] for d in self.crawl_delays):
            self.crawl_delays.append(record)


class PoliteClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        timeout: float = 15.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        limiters: dict[str, PlatformLimiter] | None = None,
        max_response_bytes: int | None = None,
        deadline_s: float | None = None,
    ):
        self.min_interval = min_interval
        # deadline_s: seconds from now after which a host's Crawl-delay is never waited out (DeadlineReached): the
        # crawl job's window, so a store asking a long delay can't hold a worker past it.
        self.deadline = None if deadline_s is None else time.monotonic() + deadline_s
        self.max_response_bytes = max_response_bytes or MAX_RESPONSE_BYTES
        # Platform-wide budgets (ratelimit.py), keyed by adapter name; for_platform() applies one.
        self.limiters = limiters or {}
        self._limiter: PlatformLimiter | None = None
        self.retries = retries
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            timeout=httpx.Timeout(timeout, pool=120.0),
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )
        self._robots: dict[str, RobotsRules | None] = {}
        # host -> its Crawl-delay record (RequestStats.crawl_delays), for a host whose robots.txt asks one
        self._delays: dict[str, dict] = {}
        # host -> (when to ask its robots.txt again, why it didn't answer)
        self._robots_down: dict[str, tuple[float, RobotsUnavailable]] = {}
        self._robots_locks: dict[
            str, threading.Lock
        ] = {}  # one robots.txt request per unknown host at a time
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._local = threading.local()  # shared by for_platform() views, so their requests are counted too

    @contextlib.contextmanager
    def track(self) -> Iterator[RequestStats]:
        """Count this thread's requests until the block ends (the client is shared by every worker thread)."""
        prev, self._local.stats = getattr(self._local, "stats", None), RequestStats()
        try:
            yield self._local.stats
        finally:
            self._local.stats = prev

    def _note(
        self,
        status: int | None,
        retry: bool = False,
        page: bool = True,
        response: httpx.Response | None = None,
    ) -> None:
        if (stats := getattr(self._local, "stats", None)) is not None:
            stats.note(status, retry, page, response)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def for_platform(self, name: str) -> PoliteClient:
        """This client with `name`'s platform-wide limiter applied to every request it sends (robots.txt too),
        on top of per-host spacing. A view sharing the connection pool and state: close the original, not it."""
        limiter = self.limiters.get(name)
        if limiter is None or limiter is self._limiter:
            return self
        view = copy.copy(self)
        view._limiter = limiter
        return view

    def with_max_response(self, max_bytes: int) -> PoliteClient:
        """This client with another response cap (sitemaps: up to 50 MB by the protocol). A view sharing the
        connection pool and state, as for_platform: close the original, not it."""
        if max_bytes == self.max_response_bytes:
            return self
        view = copy.copy(self)
        view.max_response_bytes = max_bytes
        return view

    def _send(self, url: str, params: dict | None = None) -> httpx.Response:
        if self._limiter is None:
            return self._read(
                self._client.send(self._client.build_request("GET", url, params=params), stream=True)
            )
        with self._limiter.slot(urlparse(url).hostname):
            r = self._client.send(self._client.build_request("GET", url, params=params), stream=True)
            self._limiter.record(r)  # inside the slot: a 429's cooldown is set before the next request starts
            return self._read(r)

    def _read(self, r: httpx.Response) -> httpx.Response:
        """The streamed response's body, read up to max_response_bytes (ResponseTooLarge past it), and the response
        freed as soon as its caller drops it: an httpx response refers to itself through its stream (a reference
        cycle), so without this every body waited for Python's full garbage collection, and 32 workers' pages piled
        up between collections (2026-11-02: RSS grew across a generic crawl until the OOM kill)."""
        cap = self.max_response_bytes
        try:
            size = r.headers.get("content-length", "")
            if size.isdigit() and int(size) > cap:
                raise ResponseTooLarge(
                    f"response over {cap >> 20} MiB ({int(size)} bytes): {r.url}", status=r.status_code
                )
            chunks, n = [], 0
            for chunk in r.iter_bytes():
                n += len(chunk)
                if n > cap:
                    raise ResponseTooLarge(f"response over {cap >> 20} MiB: {r.url}", status=r.status_code)
                chunks.append(chunk)
            r._content = b"".join(chunks)
        finally:
            r.close()
            r.stream = httpx.ByteStream(b"")  # breaks the response <-> stream cycle
        return r

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        with self._lock:
            known = host in self._robots
            host_lock = None if known else self._robots_locks.setdefault(host, threading.Lock())
        if host_lock is not None:
            with host_lock:  # concurrent first requests to a host share one robots.txt request
                with self._lock:
                    known = host in self._robots
                    down = self._robots_down.get(host)
                if not known:
                    if down is not None and time.monotonic() < down[0]:
                        raise RobotsUnavailable(
                            f"{down[1]} (recently); not fetching without it",
                            down[1].unreachable,
                            down[1].status,
                            down[1].challenge,
                            max(0.0, down[0] - time.monotonic()),
                        )
                    rules = self._fetch_robots(host)  # raises RobotsUnavailable: nothing is fetched
                    delay = self._crawl_delay(host, rules)
                    with self._lock:
                        self._robots[host] = rules
                        if delay is not None:
                            self._delays[host] = delay
                        self._robots_down.pop(host, None)
                        # Known from now on: no robots.txt request left to share.
                        self._robots_locks.pop(host, None)
        rp = self._robots[host]
        return rp is None or rp.can_fetch(url)

    def _crawl_delay(self, host: str, rules: RobotsRules | None) -> dict | None:
        """The Crawl-delay record for a host whose robots.txt asks one: the spacing its requests get is the larger of
        the delay, capped at MAX_CRAWL_DELAY, and min_interval. A value that isn't a number keeps min_interval."""
        if rules is None or rules.crawl_delay_asked is None:
            return None
        asked, capped = (
            rules.crawl_delay,
            rules.crawl_delay is not None and rules.crawl_delay > MAX_CRAWL_DELAY,
        )
        applied = (
            max(self.min_interval, min(asked, MAX_CRAWL_DELAY)) if asked is not None else self.min_interval
        )
        if asked is None:
            log.warning("%s: robots.txt Crawl-delay %r isn't a number; spacing stays %.1f s",
                        host, rules.crawl_delay_asked, applied)  # fmt: skip
        elif capped:
            log.warning(
                "%s: robots.txt Crawl-delay %s s capped at %.0f s", host, rules.crawl_delay_asked, applied
            )
        return {"host": host, "asked": rules.crawl_delay_asked, "applied_s": applied, "capped": capped}

    def _disallows_site(self, url: str) -> bool:
        """The host's robots.txt file disallows the whole site (/) to us: a closed store (SiteDisallowed). Not a
        robots.txt answered with 401 or 403, which is a refusal (often an edge challenge), not the store's rules."""
        rp = self._robots.get(urlparse(url).netloc)
        if rp is None or getattr(rp, "disallow_all", False):
            return False
        return not rp.can_fetch(urlparse(url)._replace(path="/", query="", fragment="").geturl())

    def _fetch_robots(self, host: str) -> RobotsRules | None:
        """The host's rules: a 200 is parsed, a 401 or 403 disallows everything, and any other 4xx (404, 400, 410...)
        means it has no robots.txt (no restrictions, RFC 9309). Anything else isn't an answer about robots.txt: an
        edge's challenge, whatever its status (SiteGround's 202, or a 200 or any 4xx, 401 and 403 included, with a
        challenge header, such as Cloudflare's 403 with `cf-mitigated: challenge`), another 2xx, a 1xx or 3xx (a
        redirect without a location), a 429, 430 or 5xx, or a network error raises RobotsUnavailable and is
        remembered for ROBOTS_RETRY_AFTER: nothing is ever fetched from a host without its rules."""
        try:
            r = self._follow(f"https://{host}/robots.txt", robots=True)
        except httpx.PoolTimeout as e:  # our own connection pool was busy: says nothing about the host
            raise RobotsUnavailable(f"robots.txt for {host} not sent ({type(e).__name__}: {e})") from e
        except ResponseTooLarge as e:
            # Not a robots.txt (a catch-all page, or a runaway one): remembered like a 5xx, so the host's later
            # fetches don't each download it again up to the cap.
            err = RobotsUnavailable(f"robots.txt for {host} too large ({e})")
            cause: Exception | None = e
        except httpx.HTTPError as e:
            # DNS failure, refused or timed-out connection to this host itself (not a host it redirected to): down.
            hostname = urlparse(f"https://{host}").hostname
            try:
                failed_host: str | None = e.request.url.host
            except RuntimeError:  # raised without a request attached
                failed_host = hostname
            unreachable = isinstance(e, httpx.ConnectError | httpx.ConnectTimeout) and failed_host == hostname
            err = RobotsUnavailable(f"robots.txt for {host} failed ({type(e).__name__}: {e})", unreachable)
            cause = e
        else:
            challenged = is_challenge(r)
            if r.status_code == 200 and not challenged:
                return RobotsRules(r.text.splitlines())
            if r.status_code in (401, 403) and not challenged:  # a challenge is no answer about the rules
                return RobotsRules(disallow_all=True)
            if 400 <= r.status_code < 500 and r.status_code not in BLOCK_STATUSES and not challenged:
                # RFC 9309's "unavailable": no robots.txt, no restrictions. Square Online's API host
                # (cdn5.editmysite.com) answers 400 here, so every Square Online catalog depends on this.
                return None
            # A challenge page standing in for robots.txt is no answer about the rules (run 2026-11-02: SiteGround's
            # 202 was read as "no robots.txt"); neither is another 2xx, a 1xx or 3xx, a 429/430 or a 5xx. The host
            # is unavailable for now.
            why = f"answered {r.status_code}" + (" (a challenge)" if challenged else "")
            block = r.status_code in BLOCK_STATUSES and not challenged
            status = r.status_code if challenged or block else None
            wait = None
            if block:  # a pause: asked again once it has passed, and the store deferred until then
                wait = retry_after_seconds(r.headers.get("retry-after"))
                wait = ROBOTS_BLOCK_RETRY_AFTER if wait is None else wait
            err = RobotsUnavailable(
                f"robots.txt for {host} {why}", status=status, challenge=challenged, wait=wait
            )
            cause = None
        # This fetch fails (the deferred rounds and the retry pass try again) and the host stays unknown, asked again
        # after ROBOTS_RETRY_AFTER (a block: its wait), so a site whose robots.txt never answers is never crawled.
        with self._lock:
            until = ROBOTS_RETRY_AFTER if err.wait is None else err.wait
            self._robots_down[host] = (time.monotonic() + until, err)
        raise RobotsUnavailable(
            f"{err}; not fetching without it", err.unreachable, err.status, err.challenge, err.wait
        ) from cause

    def _wait_turn(self, host: str) -> None:
        """Wait for the host's turn: min_interval after its last request, or its robots.txt Crawl-delay (up to
        MAX_CRAWL_DELAY) when that is longer. A Crawl-delay turn that falls after the deadline isn't waited for:
        DeadlineReached, at once, so a slow store never holds a worker past the deadline."""
        delay = self._delays.get(host)
        interval = delay["applied_s"] if delay else self.min_interval
        with self._lock:
            now = time.monotonic()
            due = self._last.get(host, 0.0) + interval
            if self.deadline is not None and interval > self.min_interval and due > self.deadline:
                raise DeadlineReached(
                    f"the run's deadline passes before {host}'s Crawl-delay ({interval:.0f} s)"
                )
            self._last[host] = max(now, due)
        if delay and (stats := getattr(self._local, "stats", None)) is not None:
            stats.note_delay(delay)
        if due > now:
            time.sleep(due - now)

    def _send_retrying(
        self, url: str, params: dict | None = None, spaced: bool = True, page: bool = True
    ) -> httpx.Response:
        """_send with the client's retries: the last response (a RETRY_STATUSES one once retries run out), or the
        last attempt's network error. spaced: per-host spacing before each attempt. Every attempt is counted in
        the thread's RequestStats; page: whether a 200 counts as a page (robots.txt doesn't)."""
        host = urlparse(url).netloc
        for attempt in range(self.retries):
            last = attempt == self.retries - 1
            if spaced:
                self._wait_turn(host)
            asked: float | None = None  # the response's Retry-After, in seconds
            try:
                r = self._send(url, params)
            except ResponseTooLarge as e:  # an answer, but never retried: counted with its status
                self._note(e.status, retry=attempt > 0, page=page)
                raise
            except httpx.HTTPError:
                self._note(None, retry=attempt > 0, page=page)
                if last:
                    raise
            else:
                self._note(r.status_code, retry=attempt > 0, page=page, response=r)
                if r.status_code not in RETRY_STATUSES or last:
                    return r
                if self._limiter is not None and r.status_code in BLOCK_STATUSES:
                    continue  # the limiter's platform-wide cooldown is the wait; don't idle this worker too
                asked = retry_after_seconds(r.headers.get("retry-after"))
                if asked is not None and asked > MAX_RETRY_WAIT:
                    return r  # never sooner than asked: the store is deferred past it (ingest.run)
            time.sleep(asked if asked is not None else 2**attempt)
        raise ValueError("retries must be >= 1")

    def _follow(self, url: str, params: dict | None = None, robots: bool = False) -> httpx.Response:
        """Follow redirects ourselves so each catalog hop checks its own robots rules and host budget.

        Robots redirects are the bootstrap exception: the redirected resource supplies the original host's rules,
        so consulting those rules before fetching it would recurse. They are still spaced and limited per host.
        """
        history: list[httpx.Response] = []
        bare = lambda u: urlparse(u)._replace(query="", fragment="").geturl()  # noqa: E731
        # URLs (without query) that redirected us away while carrying our params.
        sent_params: set[str] = set()
        for hop in range(MAX_REDIRECTS + 1):
            if not robots and not self._allowed(url):
                raise SiteDisallowed(url) if self._disallows_site(url) else Blocked(url)
            r = self._send_retrying(url, params, spaced=not robots or hop > 0, page=not robots)
            if r.status_code not in (301, 302, 303, 307, 308) or not r.headers.get("location"):
                r.history = history
                return r
            if hop == MAX_REDIRECTS:
                raise httpx.TooManyRedirects(f"too many redirects for {url}", request=r.request)
            target = urljoin(str(r.url), r.headers["location"])
            if urlparse(target).scheme not in ("http", "https"):
                raise httpx.UnsupportedProtocol(f"unsupported redirect URL {target}", request=r.request)
            # A location with its own query replaces ours; one without keeps ours, so a redirect that drops
            # page/per_page doesn't silently shrink a paged catalog. But never send our params back to a URL that
            # redirected us away with them: a site that only strips the query would loop.
            if params:
                sent_params.add(bare(str(r.url)))
            if urlparse(target).query or bare(target) in sent_params:
                params = None
            history.append(r)
            url = target
        raise AssertionError("unreachable")

    def get(self, url: str, **params: str | int | float | bool | None) -> httpx.Response:
        r = self._follow(url, params or None)
        if r.status_code in RETRY_STATUSES:
            raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
        return r

    # Every JSON body is decoded with scrub_surrogates, so a store's lone surrogate never reaches a stored row.
    def get_json(self, url: str, **params: str | int | float | bool | None) -> Any:
        """JSON body on a 200 with a JSON payload, else None (for probing, where a miss is an answer)."""
        r = self.get(url, **params)
        if r.status_code != 200:
            return None
        try:
            return scrub_surrogates(r.json())
        except ValueError:
            return None

    def catalog_json(self, url: str, **params: str | int | float | bool | None) -> Any:
        """For catalog paths that may or may not exist: None on 404 (try another path); NotCatalog on a 200
        that isn't JSON; FetchError on any other status, a network error or a robots block."""
        try:
            r = self.get(url, **params)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__} {url}") from e
        if why := unavailable_reason(r):
            raise Unavailable(why)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code} {url}", status=r.status_code)
        try:
            return scrub_surrogates(r.json())
        except ValueError as e:
            raise NotCatalog(f"not JSON: {url}") from e

    def fetch_json(self, url: str, **params: str | int | float | bool | None) -> Any:
        """JSON body, or FetchError: for catalog pages, where a miss must never look like an empty catalog."""
        try:
            r = self.get(url, **params)
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__} {url}") from e
        if why := unavailable_reason(r):
            raise Unavailable(why)
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code} {url}", status=r.status_code)
        try:
            return scrub_surrogates(r.json())
        except ValueError as e:
            raise FetchError(f"not JSON: {url}") from e
