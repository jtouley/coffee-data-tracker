"""The roaster screen's job: what a discovery candidate's site says, recorded and nothing more.

docs/design/roaster-screen.md, sections 2 and 3. For each candidate that is due, in one state, the job asks
robots.txt, the home page, and the first page of the catalog API the home page names: about three requests per
place, once. It writes raw rows only (`screens`, `screen_products`), append-only. It decides nothing: no tier, no
score and no coffee count is stored, and no place enters the registry here. dbt derives every judgement from these
rows, and the owner's flag on Sources stays the only way into the crawl.

The crawl's posture, unchanged, because every request goes through PoliteClient:
- robots.txt is read before anything else on a host and honoured; a 401 or 403 disallows everything, and a
  challenge, a 429, a 5xx or no answer means nothing is fetched from that host;
- at least 1 s between requests to a host, or its Crawl-delay up to 60 s; Shopify stores share Shopify's limiter;
- the user agent is RegistryCoffeeBot, with its contact page;
- a challenge is recorded with its edge and never worked around: no second egress, no other user agent, no retry
  beyond the client's own.

On top of that, one screen sends at most MAX_REQUESTS requests, robots.txt and redirects included, and a run stops
starting places when too many sites refuse it (Breaker).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import urlparse

import httpx

from . import discover, lineage, screen_rules
from .adapters import shopify, woocommerce
from .http import (
    CHALLENGE_STATUSES,
    Blocked,
    DeadlineReached,
    PoliteClient,
    RequestStats,
    RobotsUnavailable,
    is_challenge,
)
from .lake import Lake, new_batch_id, retry_transient
from .models import CatalogItem, Roaster
from .ratelimit import SHOPIFY_CONCURRENCY, SHOPIFY_RPS, PlatformLimiter
from .text import html_to_text

log = logging.getLogger(__name__)

# One screen's hard cap, robots.txt and redirects included (section 3.1). The worst expected path is five: an apex
# that redirects to www (two robots.txt, two hops) and one product page.
MAX_REQUESTS = 6
# The first page, at the screen's own sizes. Shopify's is the crawl's page (adapters.shopify.PAGE_LIMIT). A
# 100-product WooCommerce page can carry thousands of embedded variations and time out, so the screen asks 25 and
# steps down once to 5, as the crawl steps down (adapters.woocommerce.TIMEOUT_PER_PAGE).
PAGE_SIZES: dict[str, tuple[int, ...]] = {"shopify": (250,), "woocommerce": (25, 5)}
HOME_TEXT_CHARS = (
    50_000  # of the home page's visible text (#130's cap, which its wording rules were tuned on)
)
BODY_TEXT_CHARS = 1_500  # of each product's description, as stg_products reads
TITLE_CHARS = 300
WORKERS = 16
MAX_PER_RUN = 3_000
DEADLINE_MIN = 90.0
BATCH = 500
FLUSH_S = 600.0
ORDER_SALT = "coffeedata-screen-order"

# The circuit breaker (section 3.3). An edge on this list challenges Cloud Run's default egress whatever we do
# (SiteGround's 202 `sg-captcha`, docs/ingestion/DESIGN.md), so its challenges are reported per edge and not counted:
# they say nothing new about this run. Everything else that refuses counts, as a share of the places the run has
# finished. The threshold is provisional: the shadow run measures the real rate and the design's PR 7 sets it.
KNOWN_EDGES = ("siteground",)
BREAKER_SHARE = 0.08
BREAKER_MIN_PLACES = 100  # no share is judged on fewer first contacts than this

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


class BudgetSpent(DeadlineReached):
    """One screen's request cap is spent: what it has read is its answer."""


class ScreenClient(PoliteClient):
    """A PoliteClient that sends no request past a tracked screen's cap. Every attempt counts, as RequestStats counts
    them: robots.txt, redirects and the client's own retries. Nothing else about the client changes."""

    max_requests = MAX_REQUESTS

    def _send(self, url: str, params: dict | None = None) -> httpx.Response:
        stats = getattr(self._local, "stats", None)
        if stats is not None and stats.requests >= self.max_requests:
            raise BudgetSpent(f"the screen's {self.max_requests} requests are spent before {url}")
        return super()._send(url, params)


def client(deadline_s: float | None = None, **kw) -> ScreenClient:
    """The screen's client: the probe's pacing (1 s per host, 12 s timeout, the client's own two tries), and
    Shopify's shared budget for every Shopify request (ratelimit: 3 a second, 8 at once)."""
    limiters = {"shopify": PlatformLimiter("shopify", rate=SHOPIFY_RPS, max_concurrent=SHOPIFY_CONCURRENCY)}
    return ScreenClient(**({"min_interval": 1.0, "timeout": 12, "retries": 2, "deadline_s": deadline_s,
                            "limiters": limiters} | kw))  # fmt: skip


# --- one screen ----------------------------------------------------------------------------------------------------


def _marker(html: str) -> str | None:
    """The platform the home page's markup names (discover.HTML_MARKERS, without the probe's question mark)."""
    low = html.lower()
    return next((p.rstrip("?") for p, pat in discover.HTML_MARKERS if re.search(pat, low)), None)


def _product_row(item: CatalogItem) -> dict:
    return {
        "product_id": item.product_id,
        "handle": item.handle,
        "title": item.title,
        "vendor": item.vendor,
        "product_type": item.product_type,
        "tags": list(item.tags),
        "variants": [
            {"title": v.title, "grams": v.grams, "price": v.price_usd, "currency": v.currency,
             "available": v.available}
            for v in item.variants
        ],
        "body_hash": item.body_hash,
        "body_text": item.body_text[:BODY_TEXT_CHARS],
    }  # fmt: skip


def _parse(platform: str, data: object, r: httpx.Response, roaster: Roaster) -> tuple[int, list[dict]]:
    """(products on the page, their rows). A product the adapter can't parse is counted and left out."""
    raw = data["products"] if platform == "shopify" else data  # type: ignore[index]
    raw = [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    currency = shopify.page_currency(r) if platform == "shopify" else None
    rows = []
    for p in raw:
        try:
            item = (
                shopify.parse_product(p, roaster, currency)
                if platform == "shopify"
                else woocommerce.parse_product(p, roaster)  # no variation-price lookups: prices aren't needed
            )
        except Exception as e:  # noqa: BLE001 - one odd product is left out, never the screen
            log.debug("screen %s: a product didn't parse (%s)", roaster.roaster_id, type(e).__name__)
            continue
        rows.append(_product_row(item))
    return len(raw), rows


def _read_page(c: PoliteClient, out: dict, host: str, platform: str, roaster_id: str) -> list[dict] | None:
    """Ask one platform's first page, at its sizes in turn (a smaller one only after a timeout). The products' rows
    when the page is that platform's list, else None; what happened is written into `out`."""
    api = c.for_platform(
        platform
    )  # Shopify's shared budget; a platform without a limiter gets the client itself
    for size in PAGE_SIZES[platform]:
        try:
            data, r = discover._api(api, host, platform, size, strict=True)
        except httpx.TimeoutException:
            out["page_timeout"] = True
            continue
        out["page_timeout"] = False
        if r is not None:
            out["bytes"] += len(r.content)
            if is_challenge(r):
                out["challenged"] = True
                return None
        if data is None or r is None:
            return None
        n, rows = _parse(
            platform, data, r, Roaster(roaster_id=roaster_id, name=roaster_id, host=host, platform=platform)
        )
        out |= {"platform": platform, "page_size": size, "products_read": n, "page_full": n >= size}
        return rows
    return None


def _screen(c: PoliteClient, domain: str, out: dict) -> list[dict]:
    failed, home = discover._home(c, domain)
    if home is not None:
        out["bytes"] += len(home.content)
    if failed is not None:
        out |= failed
        out["challenged"] = home is not None and is_challenge(home)
        return []
    assert home is not None
    host = urlparse(str(home.url)).netloc
    html = home.text
    title = _TITLE.search(html)
    out |= {
        "status": "ok",
        "http_status": 200,
        "final_host": host,
        "home_title": html_to_text(title.group(1))[:TITLE_CHARS] if title else None,
        "home_text": html_to_text(html)[:HOME_TEXT_CHARS],
    }
    marker = _marker(html)
    # The API the markers name; no guess when they name another platform; both, Shopify first, when they name none.
    order = [marker] if marker in PAGE_SIZES else [] if marker else list(PAGE_SIZES)
    out["platform"] = f"{marker}?" if marker else "other"
    disallowed = False
    for platform in order:
        try:
            rows = _read_page(c, out, host, platform, domain)
        except Blocked:  # robots.txt disallows the product path
            disallowed = True
            continue
        except RobotsUnavailable as e:  # the home page redirected to a host whose robots.txt didn't answer
            out |= {"status": "blocked" if e.challenge else "robots_unavailable", "platform": "",
                    "http_status": e.status if e.challenge else None}  # fmt: skip
            return []
        if out.get("challenged"):
            out |= {"status": "blocked", "platform": ""}
            return []
        if rows is not None:
            return rows
    if disallowed:
        out |= {"status": "blocked", "http_status": None, "robots_path": True}
    return []


def _edge(stats: RequestStats) -> str | None:
    """The edge that challenged us, from the challenge's own headers (http.block_record); None without one."""
    for b in stats.blocks:
        cf = (b.get("cf_mitigated") or "").lower() == "challenge"
        if b.get("sg_captcha") is not None:
            return "siteground"
        if cf:
            return "cloudflare"
        if b.get("status") in CHALLENGE_STATUSES:
            return "other"  # a bare 202 names no edge: never guessed from the server header
    return None


def _finish(out: dict, stats: RequestStats, started: float) -> dict:
    """The row's refusal, edge and counts, from what the client recorded of this screen's requests."""
    edge = _edge(stats)
    status = out["status"]
    refusal = None
    if edge is not None and status != "ok":
        status, refusal = "blocked", "challenge"
    elif status == "blocked":
        refused = any(r.get("status") in (401, 403) for r in stats.robots)
        refusal = "robots_401_403" if refused and not out.get("robots_path") else "robots_disallow"
    elif status != "ok" and stats.statuses.get(429):
        # a 429 on the home page reaches the probe as a network error (unreachable): say what the site said
        refusal = "http_429"
        status = "http_429" if status == "unreachable" else status
    elif status == "unreachable" and (codes := sorted(s for s in stats.statuses if 500 <= s < 600)):
        status = f"http_{codes[-1]}"
    delays = [d["applied_s"] for d in stats.crawl_delays]
    for k in ("challenged", "robots_path"):
        out.pop(k, None)
    return out | {
        "status": status,
        "refusal": refusal,
        "edge": edge,
        "requests": stats.requests,
        "crawl_delay_s": max(delays) if delays else None,
        "duration_s": round(time.monotonic() - started, 3),
    }


def screen_one(c: PoliteClient, domain: str) -> tuple[dict, list[dict]] | None:
    """One place's screen: (its `screens` fields, its `screen_products` rows), or None when the run's deadline cut
    it off (it wasn't screened, so it gets no row and the next run asks it first). No other exception escapes: one
    odd site is that site's row, never the run's."""
    started = time.monotonic()
    out: dict = {"status": "unreachable", "http_status": None, "final_host": "", "platform": "", "page_size": None,
                 "products_read": 0, "page_full": False, "page_timeout": False, "home_title": None,
                 "home_text": None, "bytes": 0}  # fmt: skip
    products: list[dict] = []
    with c.track() as stats:
        try:
            products = _screen(c, domain, out)
        except BudgetSpent as e:
            log.info("screen %s: %s", domain, e)  # recorded as is, with what it has read
        except DeadlineReached:
            return None
        except Exception as e:  # noqa: BLE001 - one odd site must not abort the run
            log.warning("screen %s failed: %s", domain, type(e).__name__)
    return _finish(out, stats, started), products


# --- which places ---------------------------------------------------------------------------------------------------

CANDIDATES_SQL = """
select roaster_id, arg_max(state, (added_at, coalesce(state, ''))) as state
from {candidates} where roaster_id is not null group by roaster_id
"""
SCREENS_SQL = """
select roaster_id, screened_at, status, refusal from {screens}
where roaster_id is not null order by roaster_id, screened_at, batch_id, screen_id
"""


def order_key(domain: str) -> str:
    """A stable, salted order over the places: a run cut off by its deadline carries on where it stopped."""
    return hashlib.sha256(f"{ORDER_SALT}:{domain}".encode()).hexdigest()


def last_screens(rows: list[tuple]) -> dict[str, dict]:
    """Each place's latest screen and how many transient screens in a row it ends (screen_rules.next_due's `tries`),
    from SCREENS_SQL's rows, oldest first."""
    last: dict[str, dict] = {}
    for rid, at, status, refusal in rows:
        before = last.get(rid)
        again = before is not None and screen_rules.transient(before["status"], before["refusal"])
        tries = before["tries"] + 1 if before and again and screen_rules.transient(status, refusal) else 1
        last[rid] = {"screened_at": at, "status": status, "refusal": refusal, "tries": tries}
    return last


def select(
    lake: Lake, state: str, run_date: str, cap: int | None = MAX_PER_RUN, doc: object = None, seed=None
) -> list[str]:
    """The places due for a screen on run_date, in order (section 2.3). A place is due when it is in `candidates`
    in `state`; could be a storefront; isn't in the registry (which holds the opted-out too); carries no roaster or
    not-a-roaster flag in the served snapshot `doc`; and its latest screen's next_due is on or before run_date.
    Never-screened places go first, then the rest, each in order_key's order."""
    from . import decisions, registry

    con = lake.connect()
    cands = retry_transient(
        lambda: con.execute(CANDIDATES_SQL.format(candidates=lake.scan("candidates"))).fetchall()
    )
    screens = retry_transient(
        lambda: con.execute(SCREENS_SQL.format(screens=lake.scan("screens"))).fetchall()
    )
    last = last_screens(screens)
    doc = registry.read_served(lake) if doc is None else doc
    known = {s.roaster.roaster_id for s in registry.current(lake, seed, doc=doc)}
    flagged: set[str] = set()
    if doc:  # False: the snapshot couldn't be read, so no flags apply (as the crawl does)
        flagged = set(decisions.source_roasters(doc)) | set(decisions.not_roasters(doc))  # type: ignore[arg-type]
    day = date.fromisoformat(run_date)

    def due(rid: str) -> bool:
        s = last.get(rid)
        return (
            s is None
            or screen_rules.next_due(s["screened_at"], s["status"], s["refusal"], None, s["tries"]) <= day
        )

    todo = [
        rid
        for rid, st in cands
        if (st or "").upper() == state.upper()
        and rid not in known
        and rid not in flagged
        and registry.is_storefront_host(rid)
        and due(rid)
    ]
    todo.sort(key=lambda rid: (rid in last, order_key(rid)))
    return todo if cap is None else todo[:cap]


# --- the run --------------------------------------------------------------------------------------------------------


@dataclass
class Breaker:
    """Stops a run that too many sites refuse (section 3.3). counted: 429s, 401s and 403s without a challenge
    header (robots.txt's too), and challenges from an edge not in KNOWN_EDGES, as a share of the places finished.
    by_edge reports every challenge, the known edges' included."""

    share: float = BREAKER_SHARE
    min_places: int = BREAKER_MIN_PLACES
    places: int = 0
    counted: int = 0
    by_edge: Counter = field(default_factory=Counter)
    by_refusal: Counter = field(default_factory=Counter)

    def note(self, row: dict) -> None:
        self.places += 1
        refusal, edge, status = row.get("refusal"), row.get("edge"), row.get("status") or ""
        if refusal:
            self.by_refusal[refusal] += 1
        if refusal == "challenge":
            self.by_edge[edge or "other"] += 1
            self.counted += edge not in KNOWN_EDGES
        elif refusal in ("http_429", "robots_401_403") or status in ("http_401", "http_403", "http_429"):
            self.counted += 1

    @property
    def tripped(self) -> bool:
        return self.places >= self.min_places and self.counted > self.share * self.places

    def report(self) -> dict:
        return {"places": self.places, "counted": self.counted, "share": self.share,
                "by_edge": dict(self.by_edge), "by_refusal": dict(self.by_refusal)}  # fmt: skip


def run(
    lake: Lake,
    todo: list[str],
    state: str,
    run_date: str,
    workers: int = WORKERS,
    deadline_s: float | None = 60 * DEADLINE_MIN,
    breaker: Breaker | None = None,
    make_client: Callable[..., ScreenClient] | None = None,
    batch_size: int = BATCH,
    flush_s: float = FLUSH_S,
) -> dict:
    """Screen `todo`, each place once, and write the rows as they finish: every `batch_size` screens or `flush_s`
    seconds, and what is left at the end, even when the run stops on an error (so a stopped job keeps what it
    screened). No place is started after the deadline or once the breaker trips; those in flight finish and are
    written. Returns the totals: screened, by_status, files, left (not started or cut off), the breaker's report,
    and stopped ("refusals" when the breaker tripped, else None)."""
    breaker = breaker or Breaker()
    stop = None if deadline_s is None else time.monotonic() + deadline_s
    version = lineage.code_version()
    rd = date.fromisoformat(run_date)
    totals: dict = {"screened": 0, "products": 0, "by_status": Counter(), "files": 0, "stopped": None}
    pending: list[tuple[dict, list[dict]]] = []
    noting = threading.Lock()

    def one(c: PoliteClient, domain: str) -> tuple[dict, list[dict]] | None:
        with noting:
            if breaker.tripped:
                return None
        if stop is not None and time.monotonic() >= stop:
            return None
        got = screen_one(c, domain)
        if got is None:
            return None
        row, products = got
        with noting:  # in the worker, so the next place it would start sees this one's refusal
            breaker.note(row)
        sid = uuid.uuid4().hex
        now = datetime.now(UTC).replace(microsecond=0)
        row = {"run_date": rd, "screen_id": sid, "screened_at": now, "roaster_id": domain, "state": state,
               "code_version": version, **row}  # fmt: skip
        return row, [{"screen_id": sid, "roaster_id": domain, **p} for p in products]

    def flush() -> None:
        nonlocal pending
        batch, pending = pending, []
        if not batch:
            return
        bid = new_batch_id()
        try:
            lake.write_partition("screens", run_date, bid, [r | {"batch_id": bid} for r, _ in batch])
            lake.write_partition("screen_products", run_date, bid, [p for _, ps in batch for p in ps])
        except BaseException:
            pending = batch + pending  # not written: the flush after tries it again, under a new batch
            raise
        totals["files"] += 1

    with (make_client or client)(deadline_s=deadline_s) as c, ThreadPoolExecutor(workers) as ex:
        futures = [ex.submit(one, c, d) for d in todo]
        flushed = time.monotonic()
        try:
            for f in as_completed(futures):
                got = f.result()
                if got is None:
                    continue
                pending.append(got)
                totals["screened"] += 1
                totals["products"] += len(got[1])
                totals["by_status"][got[0]["status"]] += 1
                if len(pending) >= batch_size or time.monotonic() - flushed >= flush_s:
                    flush()
                    flushed = time.monotonic()
        except BaseException:
            for f in futures:
                f.cancel()  # start no more places
            try:
                flush()
            except Exception as e:  # noqa: BLE001 - the error that stopped the run is the one to raise
                log.error(
                    "couldn't write %d screens after the run failed: %s", len(pending), type(e).__name__
                )
            raise
        flush()
    totals["by_status"] = dict(totals["by_status"])
    totals["left"] = len(todo) - totals["screened"]
    totals["breaker"] = breaker.report()
    if breaker.tripped:
        totals["stopped"] = "refusals"
    return totals
