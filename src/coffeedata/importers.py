"""Weekly private snapshots of green-coffee importers' public offer lists (ELT: the snapshot is raw only).

Two importers, two public pages, fetched in one pass a week, never logged in, with the crawler's client
(http.PoliteClient: our user agent, robots.txt honored, bounded retries with backoff and jitter, Retry-After
respected):

- Cafe Imports, https://www.cafeimports.com/north-america/offerings (a server-rendered table; prices need a login
  and aren't on it): lot ID, name, origin, region, process, status (Spot / Afloat / At origin), estimated arrival,
  bags and bag size. No warehouse.
- InterAmerican, https://interamericancoffee.com/our-offerings/ (a wpDataTables table): warehouse, product name,
  bags, bag weight, status (AFLOAT / IN STORE). No lot ID, origin, region, process or arrival column.
- Royal Coffee, a Shopify store: its public /products.json listing, ROYAL_PAGE_LIMIT products a page, the pages
  spaced ROYAL_PAGE_INTERVAL apart, each product kept word for word under its field name (royal_cells). Fetched
  only by the scheduled workflow, never by an AI agent: see royal_listing_url.

Stored (load): one immutable file per importer per week, `raw/week=<Monday>/importer=<name>.parquet`
(importer_raw), in a private Hugging Face dataset of its own (`COFFEEDATA_IMPORTERS`, created private by
`ensure_private` with the existing credential). Each lot is kept word for word, as the roaster adapters keep a
product: every cell of its row under the page's header, and the lot's own page URL. Columns about prices are left
out by their header (a header-less column, by what all its cells say), and a currency amount in any kept cell fails
the run, so no price is ever stored. No page HTML or images either.

Derived (transform): a private dbt project of its own (dbt_importers/, `build()`, `coffeedata importer-build`) reads
the whole raw history into its own database file (build/importers.duckdb) and derives each lot's stated fields
(`stated`, called from a dbt Python model) and our model's normalized values (SQL rules over importer_rules'
vocabulary), so a better rule re-derives every week. Those rules abstain when unsure and aren't evaluated yet
(CLAUDE.md): nothing relies on them. After each weekly snapshot the workflow builds it and `publish_derived` keeps
the latest build's importer_offers in the same private dataset, as one file (DERIVED) replaced at each build: a
copy of the latest derivation, never an input to anything, and never read back by the build.

Backend-only, for internal use and a future roaster portal (which may show only the normalized values, never the
importer's wording): the main dbt project, the release, the public tables and contracts, the site, the public dataset
and the MCP server never read the dataset or the private database (tests/test_importers.py). A week already stored is
never fetched again.

A changed page fails loudly (LayoutChanged), and that importer's week stays unwritten so a run later that week
fetches it again:
- a missing table or column, zero rows, or more than MAX_BAD_SHARE of rows without their identity (lot ID, name);
- a currency amount in a stored cell;
- fewer than MIN_SHARE_OF_LAST_WEEK of the rows the last stored week had (a thinned page), unless the run is told to
  accept the drop (`--accept-drop`, the workflow's `accept_drop` input).
A single cell that can't be read (bags that aren't a number) is null in the derived view and counted; it never fails
the week.

What these snapshots are for (arrival-to-roaster lag, after a year or more of history): the green-importer
feasibility check, recommendation 2. .github/workflows/importers.yml runs it each week.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import pyarrow.parquet as pq

from .http import FetchError, PoliteClient, challenge_error
from .lake import Lake, _same_file

log = logging.getLogger(__name__)

PARSER_VERSION = "2"
# A page where more than this share of rows lack their identity (lot ID, name) has changed under the parser.
MAX_BAD_SHARE = 0.10
# A week with fewer rows than this share of the importer's last stored week is a thinned page (pagination, a filter
# default, a lazy load), not a real market move: the run fails unless told to accept the drop. Week-to-week swings
# on these lists are far smaller (lots arrive and sell out over weeks); halving in one week is a page change.
MIN_SHARE_OF_LAST_WEEK = 0.5
# Waits between tries of a Hub call this job can't do without, on a transient failure only (`_transient`).
HUB_RETRY_WAITS = (2.0, 6.0, 18.0)


class LayoutChanged(Exception):
    """The page no longer looks like the one the parser was written for."""


# --- a small table reader (stdlib only) ---------------------------------------------------------------------------


@dataclass
class Table:
    attrs: dict[str, str]
    header: list[str]
    rows: list[list[list[str]]]  # each row's cells; each cell's text in lines (split at <br>)
    links: list[list[str]] = field(default_factory=list)  # each row's link targets (href), in page order


class _TableReader(HTMLParser):
    """Every <table>'s header and rows, as text. Text inside `class="hide"` elements, scripts and styles is left out;
    a <br> starts a new line within a cell. Nested tables are read as tables of their own."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self._stack: list[tuple[Table, list | None, dict | None]] = []  # (table, open row, open cell)
        self._hidden: list[bool] = []  # one per open element that can hide text, True where it does
        self._skip = 0  # inside <script> or <style>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("span", "div", "small", "em", "a", "strong", "b", "i"):
            self._hidden.append("hide" in a.get("class", "").split())
            if tag == "a" and a.get("href") and self._stack and (cell := self._stack[-1][2]) is not None:
                cell["links"].append(a["href"])
        elif tag == "table":
            self._stack.append((Table(a, [], []), None, None))
        elif not self._stack:
            return
        elif tag == "tr":
            t, _, _ = self._stack[-1]
            self._stack[-1] = (t, [], None)
        elif tag in ("td", "th"):
            t, row, _ = self._stack[-1]
            if row is None:  # a cell outside a <tr>: start one
                row = []
            self._stack[-1] = (t, row, {"tag": tag, "lines": [""], "links": []})
        elif tag == "br":
            _, _, cell = self._stack[-1]
            if cell is not None:
                cell["lines"].append("")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("span", "div", "small", "em", "a", "strong", "b", "i"):
            if self._hidden:
                self._hidden.pop()
        elif not self._stack:
            return
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            t, row, _ = self._stack[-1]
            if row:
                if all(c["tag"] == "th" for c in row) and not t.header:
                    t.header = [_text(c["lines"]) for c in row]
                elif any(c["tag"] == "td" for c in row):
                    t.rows.append([[_clean(x) for x in c["lines"] if _clean(x)] for c in row])
                    t.links.append([href for c in row for href in c["links"]])
            self._stack[-1] = (t, None, None)
        elif tag == "table":
            self._close_cell()
            self.tables.append(self._stack.pop()[0])

    def _close_cell(self) -> None:
        t, row, cell = self._stack[-1]
        if cell is not None and row is not None:
            row.append(cell)
        self._stack[-1] = (t, row, None)

    def handle_data(self, data: str) -> None:
        if self._skip or any(self._hidden) or not self._stack:
            return
        _, _, cell = self._stack[-1]
        if cell is not None:
            cell["lines"][-1] += data


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _text(lines: list[str]) -> str:
    return _clean(" ".join(lines))


def read_tables(html: str) -> list[Table]:
    reader = _TableReader()
    reader.feed(html)
    reader.close()
    return reader.tables


def find_table(html: str, required: tuple[str, ...]) -> Table:
    """The first table whose header has every `required` column (case-insensitive); LayoutChanged if none does."""
    want = {c.casefold() for c in required}
    for t in read_tables(html):
        if want <= {h.casefold() for h in t.header}:
            return t
    raise LayoutChanged(f"no table with the columns {', '.join(required)}")


@dataclass
class Lot:
    """One table row as stored: every cell under the page's header, word for word, and the lot's own page URL."""

    row: int
    cells: dict[str, str | None]
    url: str | None


# A header naming a price or cost: that column is never stored.
PRICE_HEADER = re.compile(
    r"\bprices?\b|\bcosts?\b|\$|/\s*lb\b|\bfob\b|\bdifferentials?\b|\bdiff\b|\bpremium\b", re.IGNORECASE
)
# A header-less column whose every non-empty cell is about prices ("Log In For Price") is a price column too.
PRICE_TEXT = re.compile(r"\bprices?\b|\bpricing\b", re.IGNORECASE)
# A price in a kept cell means one got past the column filter, so the run fails: a currency symbol or code on either
# side of a number ("$4.25", "4,50 €", "USD 4.10", "4.10 usd"), an amount per weight ("4.25/lb", "310 c/lb",
# "4.10 per kg"), or a labelled price ("Price: 4.25"). Not caught: a bare number in a column whose header doesn't
# say price (e.g. "4.10" under "Offer"); such a column has to be added to PRICE_HEADER.
CURRENCY = re.compile(
    r"[$€£¢]\s*\d|\d\s*[$€£¢]|\b(?:USD|EUR|GBP)\s*\d|\d\s*(?:USD|EUR|GBP)\b"
    r"|\d\s*(?:c|cts|cents?|¢)?\s*(?:/|per\s+)\s*(?:lbs?|kgs?|kilos?|pounds?)\b"
    r"|\bprices?\s*[:=]\s*[$€£]?\s*\d",
    re.IGNORECASE,
)


def _verbatim(lines: list[str]) -> str | None:
    """A cell's text as the page shows it, line by line (whitespace runs collapsed, as a browser does)."""
    return "\n".join(lines) or None


def unique(names: list[str]) -> list[str]:
    """Header names made unique, so no cell is lost under a repeated one: the second "Name" is "Name (2)"."""
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out


def price_columns(table: Table, names: list[str]) -> set[int]:
    """The columns left out as prices: by header, or, for a header-less column, by what all its cells say."""
    out = {i for i, n in enumerate(names) if PRICE_HEADER.search(n)}
    for i, h in enumerate(table.header):
        texts = [" ".join(r[i]) for r in table.rows if i < len(r) and r[i]]
        if not h and texts and all(PRICE_TEXT.search(x) for x in texts):
            out.add(i)
    return out


def lots(html: str, required: tuple[str, ...], page: str) -> tuple[list[Lot], int]:
    """Every row of the page's offer table as a Lot, and how many rows had the wrong number of cells (not kept)."""
    table = find_table(html, required)
    names = unique([h or f"column {i + 1}" for i, h in enumerate(table.header)])  # a header cell with no text
    drop = price_columns(table, names)
    out: list[Lot] = []
    bad = 0
    for k, row in enumerate(table.rows):
        if len(row) != len(names):
            bad += 1
            continue
        cells = {names[i]: _verbatim(lines) for i, lines in enumerate(row) if i not in drop}
        for col, text in cells.items():
            if text and CURRENCY.search(text):
                raise LayoutChanged(f"a currency amount in the {col!r} column: prices are never stored")
        links = table.links[k] if k < len(table.links) else []
        out.append(Lot(row=len(out), cells=cells, url=lot_url(links, page)))
    return out, bad


def lot_url(links: list[str], page: str) -> str | None:
    """The lot's own page on the importer's site: the row's first link on the same host, resolved against the
    offer page (never a login or portal link)."""
    host = urlsplit(page).hostname
    for href in links:
        url = urljoin(page, href)
        parts = urlsplit(url)
        if (
            parts.scheme in ("http", "https")
            and parts.hostname == host
            and not re.search(r"login|portal", url, re.I)
        ):
            return url
    return None


# --- the stated fields, read from the stored cells ----------------------------------------------------------------

STATUSES = {"spot": "spot", "afloat": "afloat", "at origin": "at_origin", "in store": "in_store"}
LB_KG = 0.45359237
LOT_ID = re.compile(r"\d+(?:-\d+)?")  # a number; a lot split for another shipment has a suffix: "25748-2"


def _whole(text: str | None) -> str | None:
    return _clean(" ".join((text or "").split("\n"))) or None


def _first_line(text: str | None) -> str:
    return (text or "").split("\n")[0]


def status(text: str) -> str | None:
    """A status as one of STATUSES' values; a value we haven't seen is kept as the page writes it, lowercased."""
    t = _clean(text).casefold()
    return STATUSES.get(t, t) or None


def bag_kg(text: str) -> float | None:
    """The bag's weight in kg from text like "60 Kg", "59 Kg Bags", "30 lb" or "1MT Supersacks" (a metric tonne);
    None when it doesn't say."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kgs?|kilos?|lbs?|mt)\b", text, re.IGNORECASE)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    return round(n * LB_KG, 2) if unit.startswith("lb") else n * 1000 if unit == "mt" else n


def _bags(text: str | None, problems: list[str]) -> int | None:
    t = _whole(text) or ""
    if re.fullmatch(r"\d[\d,]*", t):
        return int(t.replace(",", ""))
    problems.append("bags")
    return None


STATED_FIELDS = (
    "lot_id", "name", "origin", "region", "process", "status", "est_arrival", "warehouse", "bags", "bag_size", "bag_kg",
)  # fmt: skip


def stated(importer: str, cells: dict[str, str | None], problems: list[str] | None = None) -> dict:
    """The fields the page states for one lot, read from its stored cells. A cell that can't be read is None and
    noted in `problems`; `lot_id` and `name` are None when the row lacks its identity.

    Royal's listing has no column for any of them but its title (the name): its lot ID, warehouse, status, arrival,
    bags, origin and process are read from its title, tags and variants by the private dbt project's rules
    (int_royal_rules)."""
    problems = [] if problems is None else problems
    if importer == ROYAL:
        return dict.fromkeys(STATED_FIELDS) | {"name": " ".join((cells.get("title") or "").split()) or None}
    g = cells.get
    if importer == "cafe_imports":
        lot = _whole(g("ID"))
        # "Afloat / Est Arrival: Early Jan 2027" -> "Early Jan 2027"
        arrival = re.sub(r"^.*?est\.? arrival:?\s*", "", _whole(g("Est Arrival")) or "", flags=re.IGNORECASE)
        return {
            "lot_id": lot if lot and LOT_ID.fullmatch(lot) else None,
            "name": _whole(g("Name")),
            "origin": _whole(g("Origin")),
            "region": _whole(g("Region")),
            "process": _whole(g("Process")),
            "status": status(_first_line(g("Location"))),
            "est_arrival": arrival or None,
            "warehouse": None,
            "bags": _bags(g("Bags"), problems),
            "bag_size": _whole(g("Bag Size")),
            "bag_kg": bag_kg(_whole(g("Bag Size")) or ""),
        }
    return {
        "lot_id": None,
        "name": _whole(g("Product")),
        "origin": None,
        "region": None,
        "process": None,
        "status": status(_whole(g("Status")) or ""),
        "est_arrival": None,
        "warehouse": _whole(g("Location/Destination")),
        "bags": _bags(g("Available Bags"), problems),
        "bag_size": _whole(g("Bag Weight")),
        "bag_kg": bag_kg(_whole(g("Bag Weight")) or ""),
    }


def identified(importer: str, fields: dict) -> bool:
    """Whether a lot has its identity: a name, and on Cafe Imports a lot ID."""
    return bool(fields["name"]) and (importer != "cafe_imports" or bool(fields["lot_id"]))


@dataclass
class Parsed:
    lots: list[Lot]
    unreadable_rows: int  # rows without their identity, or with the wrong number of cells
    bad_cells: int  # cells that read as null (bags that aren't a number)
    dropped_prices: int = 0  # Royal: fields whose value mentioned a price, left out (parse_royal)


def parse(importer: str, html: str) -> Parsed:
    """The page's lots, checked: LayoutChanged on zero rows or more than MAX_BAD_SHARE without their identity."""
    imp = IMPORTERS[importer]
    assert imp.url is not None
    found, bad = lots(html, imp.columns, imp.url)
    return checked(importer, found, bad)


def checked(importer: str, found: list[Lot], bad: int) -> Parsed:
    """`found` as Parsed, after the checks every importer gets: LayoutChanged on zero rows or more than MAX_BAD_SHARE
    without their identity; a cell that reads as null is counted."""
    bad_cells = 0
    for lot in found:
        problems: list[str] = []
        if not identified(importer, stated(importer, lot.cells, problems)):
            bad += 1
        bad_cells += len(problems)
    total = len(found) + bad
    if not found or len(found) == bad:
        raise LayoutChanged(f"{importer}: zero readable rows ({bad} unreadable)")
    if bad / total > MAX_BAD_SHARE:
        raise LayoutChanged(f"{importer}: {bad} of {total} rows unreadable (more than {MAX_BAD_SHARE:.0%})")
    if bad or bad_cells:
        log.warning("%s: %d of %d rows unreadable, %d cells null", importer, bad, total, bad_cells)
    return Parsed(found, bad, bad_cells)


# --- Royal Coffee: a Shopify storefront's public product listing ---------------------------------------------------
#
# Royal Coffee's terms restrict automated access, single out AI tools, and restrict storing content; the owner chose
# to include it and accepts that risk. To respect the terms' AI clause, no AI agent ever fetches the site: only the
# scheduled importers workflow, a plain program, does, once a week. So the store's address is not in this code at all.
# The workflow passes it in ROYAL_URL_VAR on a scheduled run's first attempt only, and royal_listing_url() refuses it
# anywhere else (outside GitHub Actions, in another workflow, on a dispatch, on a re-run, or under pytest). That check
# reads environment values any local process can set: it stops accidents, not a deliberate run. Every client but the
# importers job's also refuses the host outright (http.schedule_only_host), and so do the roaster registry and
# discovery. Tests use invented products and a made-up host (tests/fixtures/importers/royal_synthetic_products.json).

ROYAL = "royal_coffee"
ROYAL_URL_VAR = "COFFEEDATA_ROYAL_URL"
# The importers workflow's name, as GITHUB_WORKFLOW reports it.
ROYAL_WORKFLOW = "importers"
# The most products a /products.json page holds (adapters.shopify.PAGE_LIMIT, from Shopify's REST pagination docs), so
# the listing takes the fewest pages.
ROYAL_PAGE_LIMIT = 250
# A listing that needs more pages than this has changed under us (or pages forever): fail, don't keep asking. Sized for
# a large Shopify catalog: Royal's listing holds more than 5,000 products (20 pages of 250 weren't enough), sold-out
# lots included. At ROYAL_PAGE_INTERVAL a full pass of 100 pages takes about 25 minutes, well inside
# IMPORTERS_DEADLINE_S. At the longest Crawl-delay the client honors (60 s) only about 90 pages fit in the deadline
# beside the other importers, so a full 100-page pass then ends as a recorded DeadlineReached, nothing written
# (tests/test_importer_royal.py).
ROYAL_MAX_PAGES = 100
# The weekly command's whole run (cli.cmd_importer_snapshot's client deadline): no request is sent after this many
# seconds, so a pass that runs long fails as DeadlineReached, recorded, nothing written, well inside the workflow's
# 120-minute job timeout (the checkout, uv sync and the upload take the rest).
IMPORTERS_DEADLINE_S = 105 * 60
# Seconds read_royal waits before each listing page after the first. The client's own per-host spacing applies on
# top: its min_interval, or the store's robots.txt Crawl-delay up to http.MAX_CRAWL_DELAY (60 s).
ROYAL_PAGE_INTERVAL = 15.0
# The owner's one-week override for a real drop at Royal (a repository variable, given to a scheduled first attempt
# only): the Monday of the week whose thinned listing may be stored, as YYYY-MM-DD.
ROYAL_ACCEPT_DROP_VAR = "ROYAL_ACCEPT_DROP_WEEK"


class NotAllowedHere(RuntimeError):
    """Royal's address was given to something other than the scheduled importers workflow's first attempt."""


def _scheduled_first_attempt(env: dict[str, str]) -> bool:
    """Whether this process is the importers workflow's scheduled run, on its first attempt (a re-run keeps the
    original event, so the attempt number tells it apart)."""
    return (
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_WORKFLOW") == ROYAL_WORKFLOW
        and env.get("GITHUB_EVENT_NAME") == "schedule"
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
    )


def royal_listing_url(env: dict[str, str] | None = None) -> str | None:
    """Royal's /products.json URL on the importers workflow's scheduled run, first attempt; None anywhere else without
    the address (the importer is skipped: a laptop, a test, a dispatch, a re-run). NotAllowedHere when the address is
    set anywhere but there (or under pytest), and when that run lacks it (a workflow edit gone wrong must not pass
    as a skip). Nothing else in the code knows the address."""
    import sys

    env = dict(os.environ) if env is None else env
    base = (env.get(ROYAL_URL_VAR) or "").strip()
    scheduled = _scheduled_first_attempt(env)
    if not base:
        if scheduled:
            raise NotAllowedHere(
                f"{ROYAL_URL_VAR} is empty on the scheduled run's first attempt; check the workflow"
            )
        return None
    if not scheduled:
        raise NotAllowedHere(
            f"{ROYAL_URL_VAR} is read only on the scheduled {ROYAL_WORKFLOW} workflow's first attempt"
        )
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        raise NotAllowedHere(f"{ROYAL_URL_VAR} is set under pytest; tests never reach Royal's site")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname or parts.path.strip("/") or parts.query:
        raise NotAllowedHere(f"{ROYAL_URL_VAR} must be a bare https:// origin")
    return f"https://{parts.hostname}/products.json"


def royal_accepts_drop(week: date, env: dict[str, str] | None = None) -> tuple[bool, str | None]:
    """(accept, note): whether the owner's ROYAL_ACCEPT_DROP_VAR accepts a thinned Royal listing this week. Only on the
    scheduled run's first attempt, and only when it names this week's Monday or the previous week's: the week whose
    run failed, which the failure message names, so the next weekly run (a week later) can use it. Any older or
    other value is ignored, with a note for the result, so it expires by itself."""
    env = dict(os.environ) if env is None else env
    value = (env.get(ROYAL_ACCEPT_DROP_VAR) or "").strip()
    if not value:
        return False, None
    if not _scheduled_first_attempt(env):
        return False, f"{ROYAL_ACCEPT_DROP_VAR} ignored: read only on the scheduled run's first attempt"
    accepted = (week.isoformat(), (week - timedelta(days=7)).isoformat())
    if value not in accepted:
        return False, (
            f"{ROYAL_ACCEPT_DROP_VAR}={value!r} ignored: it isn't this week's or last week's Monday "
            f"({' or '.join(accepted)})"
        )
    return True, None


def read_royal(
    client: PoliteClient, url: str, last: int | None = None, sleep: Callable[[float], None] | None = None
) -> Parsed:
    """Every product in the store's public listing, one page after another at ROYAL_PAGE_LIMIT a page and
    ROYAL_PAGE_INTERVAL apart, with the crawler's client (our user agent, robots.txt honored,
    bounded retries; never logged in). Then parse_royal.

    - An empty page ends the listing.
    - A short page ends it only once the products read reach `last` (the last stored week's count). Shopify can
      return short pages with more behind them (adapters/shopify.py), so before that the next page is asked for
      too. A listing of N products takes ceil(N / 250) pages when it hasn't shrunk, one more otherwise.
    - A page that repeats a product already read means the store ignores `page`: LayoutChanged, since reading on
      would store a truncated listing as a whole week. So is needing more than ROYAL_MAX_PAGES pages.
    - Products without an id are kept apart, each counted on its own (never merged under one key)."""
    products: dict[str, dict] = {}
    no_id: list[dict] = []
    for page in range(1, ROYAL_MAX_PAGES + 1):
        if page > 1:
            (sleep or time.sleep)(ROYAL_PAGE_INTERVAL)
        data = client.fetch_json(url, limit=ROYAL_PAGE_LIMIT, page=page)
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise LayoutChanged(f"{ROYAL}: page {page} is not a /products.json listing")
        batch = [p for p in data["products"] if isinstance(p, dict)]
        if not data["products"]:
            break
        keyed = {str(p["id"]): p for p in batch if p.get("id") not in (None, "")}
        if not keyed and len(data["products"]) >= ROYAL_PAGE_LIMIT:
            raise LayoutChanged(f"{ROYAL}: page {page} is full but holds no product with an id; not stored")
        if repeated := keyed.keys() & products.keys():
            raise LayoutChanged(
                f"{ROYAL}: page {page} repeats {len(repeated)} products already read (the store ignores `page`?); "
                "not stored, so a truncated listing is never a whole week"
            )
        products |= keyed
        no_id += [p for p in batch if p.get("id") in (None, "")]
        if (
            len(data["products"]) < ROYAL_PAGE_LIMIT
            and last is not None
            and len(products) + len(no_id) >= last
        ):
            break
    else:
        raise LayoutChanged(f"{ROYAL}: more than {ROYAL_MAX_PAGES} pages of {ROYAL_PAGE_LIMIT}; not stored")
    return parse_royal([*products.values(), *no_id], url)


# The product fields kept, by name (never by what their text says): anything else the listing carries, prices and
# images included, is never stored. As the roaster adapters keep a Shopify product (snapshots and bodies), with the
# body's text from body_html (text.html_to_text: its words, without markup or image tags).
ROYAL_PRODUCT_FIELDS = ("id", "title", "handle", "vendor", "product_type", "tags", "published_at")
ROYAL_VARIANT_FIELDS = ("id", "title", "option1", "option2", "option3", "sku", "available", "grams")
ROYAL_OPTION_FIELDS = ("name", "values")


def royal_cells(p: dict) -> dict[str, str | None]:
    """One product word for word, as {field: text}: each kept field's value (tags one per line; options and variants
    as JSON with only their kept fields), and body_html's text. Null where the store gives nothing."""
    import json

    from .text import html_to_text

    def text(v: object) -> str | None:
        if v is None or v == "" or v == []:
            return None
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return "\n".join(v)
        if isinstance(v, list | dict):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    cells = {f: text(p.get(f)) for f in ROYAL_PRODUCT_FIELDS}
    cells["body_text"] = html_to_text(p.get("body_html")) or None
    cells["options"] = text(
        [{k: o.get(k) for k in ROYAL_OPTION_FIELDS} for o in p.get("options") or [] if isinstance(o, dict)]
    )
    cells["variants"] = text(
        [{k: v.get(k) for k in ROYAL_VARIANT_FIELDS} for v in p.get("variants") or [] if isinstance(v, dict)]
    )
    return cells


def parse_royal(products: list[dict], url: str) -> Parsed:
    """The listing's products as Lots, checked like every importer's rows (`checked`): a product without an id, title
    or handle lacks its identity, each one counted. A field whose value mentions a price (CURRENCY: a product's prose
    can mention a premium paid) is left out of that product, never stored, and counted; more than MAX_BAD_SHARE of
    products with such a field fails the run, as too many unreadable rows do."""
    parts = urlsplit(url)
    found: list[Lot] = []
    bad = dropped = priced = 0
    for p in products:
        if not p.get("id") or not str(p.get("title") or "").strip() or not p.get("handle"):
            bad += 1
            continue
        cells = royal_cells(p)
        hits = [col for col, text in cells.items() if text and CURRENCY.search(text)]
        for col in hits:
            cells[col] = None
        dropped += len(hits)
        priced += bool(hits)
        found.append(
            Lot(row=len(found), cells=cells, url=f"{parts.scheme}://{parts.hostname}/products/{p['handle']}")
        )
    if found and priced / len(found) > MAX_BAD_SHARE:
        raise LayoutChanged(
            f"{ROYAL}: {priced} of {len(found)} products mention a price (more than {MAX_BAD_SHARE:.0%}); not stored"
        )
    if dropped:
        log.warning("%s: %d fields of %d products mentioned a price, left out", ROYAL, dropped, priced)
    parsed = checked(ROYAL, found, bad)
    parsed.dropped_prices = dropped
    return parsed


@dataclass(frozen=True)
class Importer:
    url: str | None
    columns: tuple[str, ...] = ()  # the columns its table must have
    # A source that isn't one HTML page: where it is this run (None: skip it) and how to read it.
    locate: Callable[[], str | None] | None = None
    read: Callable[[PoliteClient, str, int | None], Parsed] | None = (
        None  # (client, url, last stored week's rows)
    )

    def where(self) -> str | None:
        return self.locate() if self.locate else self.url


IMPORTERS = {
    "cafe_imports": Importer(
        "https://www.cafeimports.com/north-america/offerings",
        ("Origin", "Name", "ID", "Bag Size", "Bags", "Location", "Est Arrival", "Region", "Process"),
    ),
    "interamerican": Importer(
        "https://interamericancoffee.com/our-offerings/",
        ("Location/Destination", "Product", "Available Bags", "Bag Weight", "Status"),
    ),
    ROYAL: Importer(None, locate=royal_listing_url, read=read_royal),
}
# Importers only the scheduled workflow run fetches: never offered on a dispatch, and no drop of theirs is accepted
# by one (cli.cmd_importer_snapshot).
SCHEDULE_ONLY = (ROYAL,)


# --- the derived view: a private dbt project with a database file of its own --------------------------------------

DBT_PROJECT = "dbt_importers"
# The private database's one name and place, relative to the checkout (ROOT), wherever the command runs from: the
# build writes nowhere else, and the project itself refuses any other target (dbt_importers/macros/private_target.sql;
# importers.build runs dbt from ROOT).
DB = "build/importers.duckdb"
# Databases and folders a public job reads or uploads: the main build's database (cli.cmd_build's default and
# COFFEEDATA_DB; the MCP server's run_sql reads it), the served release's (the site and the public export), the site
# and the public export's folders. The private database is never one of them, nor inside one, whether they are read
# from the checkout or from where the command runs.
MAIN_DB = "build/coffee.duckdb"
SERVED_DB = "build/served.duckdb"
PUBLIC_FOLDERS = ("build/site", "build/public")
# dbt's own work tables beside a model's (a table materialization builds `<model>__dbt_tmp` and keeps the old one as
# `<model>__dbt_backup` until it swaps them): an interrupted build can leave them behind.
DBT_WORK_SUFFIXES = ("__dbt_tmp", "__dbt_backup")


def _checkout() -> Path:
    """The checkout this module runs from (the folder holding dbt_importers/), else the working directory (an
    installed package)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / DBT_PROJECT).is_dir() else Path.cwd().resolve()


# Where DB lives: the checkout's root, so the private database is one file whatever directory a command runs from.
ROOT = _checkout()


def _project_dir() -> Path:
    """The checkout's private project, wherever this runs from."""
    project = Path(__file__).resolve().parents[2] / DBT_PROJECT
    return project if project.is_dir() else ROOT / DBT_PROJECT


def own_table(name: str, models: set[str]) -> bool:
    """A table the private database may hold: one of the project's models, or dbt's work table for one (left by an
    interrupted build, and replaced by the next)."""
    return name in models or any(name.endswith(s) and name[: -len(s)] in models for s in DBT_WORK_SUFFIXES)


def private_models() -> set[str]:
    """The names of the private project's models: the only tables its database may hold."""
    return {p.stem for p in (_project_dir() / "models").rglob("*") if p.suffix in (".sql", ".py")}


def private_db() -> Path:
    """The private database's path, DB under the checkout (ROOT), whatever the working directory, after checking that
    neither it nor a folder on its way from ROOT is a link (the fixed path is the file itself, never somewhere else),
    and that it is none of the public databases or folders, read from the checkout and from the working directory.
    ValueError otherwise."""
    root = ROOT.resolve()
    db = root / DB
    rel = Path(DB)
    for part in (rel, *list(rel.parents)[:-1]):
        if (root / part).is_symlink():
            raise ValueError(
                f"{root / part} is a link; the importers' private database is a file of its own, at {db}"
            )
    bases = {root, Path.cwd().resolve()}
    named = (MAIN_DB, SERVED_DB, os.environ.get("COFFEEDATA_DB") or MAIN_DB)
    public = {(b / p).resolve() for b in bases for p in named}
    if db in public:
        raise ValueError(f"{db} is a public build's database; the importers' derived view has its own")
    folders = [(b / f).resolve() for b in bases for f in PUBLIC_FOLDERS]
    if (export := os.environ.get("PUBLIC_EXPORT_URL")) and "://" not in export:
        folders.append(Path(export).resolve())
    if any(db.is_relative_to(f) for f in folders):
        raise ValueError(f"{db} is inside a folder a public job uploads")
    return db


def build(lake: Lake, target: Path | None = None, unit_tests: bool = False, log: Path | None = None) -> bool:
    """Build the private dbt project (dbt_importers/) over the importers dataset into its own database file (DB under
    the checkout, `private_db`), rebuilt from raw each time. Reads the dataset (its staged files, and the Hub when it
    is remote), never writes it. A database already there is replaced only when it holds nothing but the private
    project's tables, or dbt's work tables for them that an interrupted build left (ValueError otherwise: it isn't
    ours to delete).

    The rules are SQL over fixed patterns, so they don't fail lot by lot as the Python rules could: an error in one
    (a pattern RE2 rejects, a model that no longer compiles) fails the whole build, loudly, and leaves no database
    behind, so no stale or half-built view is ever read. dbt's unit tests check the rules over hand-written rows, so
    they run only when asked (the tests). With `log`, dbt's own output goes to that file instead of the console:
    dbt prints a failing query's error, and a DuckDB error can quote a value it read, so the workflow (a public log)
    keeps it on the runner and prints only `run_counts`. Returns whether every model and check passed."""
    import json
    import subprocess

    import duckdb

    db = private_db()
    if db.exists():
        with duckdb.connect(str(db), read_only=True) as con:
            tables = {
                t for (t,) in con.execute("select table_name from information_schema.tables").fetchall()
            }
        models = private_models()
        if foreign := {t for t in tables if not own_table(t, models)}:
            raise ValueError(
                f"{db} holds tables the importers project doesn't own ({', '.join(sorted(foreign))})"
            )
        db.unlink()  # derived: always rebuilt from raw
    db.with_name(db.name + ".wal").unlink(missing_ok=True)  # a killed build's write-ahead log, never replayed
    db.parent.mkdir(parents=True, exist_ok=True)
    # dbt runs from the checkout, where DB is the one relative path the project accepts; everything else it's given
    # is absolute, so the caller's working directory doesn't matter
    env = {k: v for k, v in os.environ.items() if k != "COFFEEDATA_DB"} | {"COFFEEDATA_IMPORTERS_DB": DB}
    project_dir = str(_project_dir().resolve())
    where = ["--target-path", str(target.resolve())] if target else []
    skip = [] if unit_tests else ["--exclude", "resource_type:unit_test"]
    files = [s if "://" in s else str(Path(s).resolve()) for s in lake.sources("importer_raw")]
    sources = json.dumps({"importer_sources": files})
    cmd = ["dbt", "build", *skip, "--project-dir", project_dir, "--profiles-dir", project_dir, *where, "--vars", sources]  # fmt: skip
    if log is None:
        r = subprocess.run(cmd, env=env, cwd=ROOT)
    else:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as out:
            r = subprocess.run(cmd, env=env, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        db.unlink(missing_ok=True)  # never leave a half-built view
        db.with_name(db.name + ".wal").unlink(missing_ok=True)
    return r.returncode == 0


def run_counts(target: Path) -> dict[str, dict[str, int]]:
    """How many of the build's nodes ended in each status, by kind (model, test, unit_test, ...), from dbt's
    run_results.json in `target`: counts only. A node's message is never read, since a failing query's error can
    quote a value it read. Empty when dbt wrote no results (it stopped before running anything)."""
    import json

    try:
        results = json.loads((target / "run_results.json").read_text())["results"]
    except (OSError, ValueError, KeyError):
        return {}
    counts: dict[str, dict[str, int]] = {}
    for r in results:
        kind = str(r.get("unique_id", "")).split(".")[0] or "unknown"
        status = str(r.get("status"))
        counts.setdefault(kind, {}).setdefault(status, 0)
        counts[kind][status] += 1
    return counts


# The derived copy in the private importers dataset: the latest build's importer_offers, one file, replaced at each
# build (publish_derived). Nothing else is ever written under derived/ (ensure_private refuses a dataset holding
# anything else there).
DERIVED = "derived/importer_offers.parquet"
DERIVED_FILES = {DERIVED}


def publish_derived(lake: Lake, sha: str | None = None) -> dict:
    """Send the private database's importer_offers (a build that passed: `build`) to the private importers dataset
    as DERIVED, replacing the file there, after the same checks as the snapshot (`ensure_private`). Only to a
    remote (Hugging Face) dataset: ValueError otherwise. The file is skipped when the dataset already holds the same
    bytes. Returns counts only (lots, lots per importer, weeks, the latest week's Monday) and whether it was sent:
    the workflow prints them to a public log, so never a lot's value."""
    import duckdb

    if not lake.remote:
        raise ValueError(f"publish_derived sends to the private Hugging Face dataset only, not {lake.url!r}")
    db = private_db()
    if not db.exists():
        raise FileNotFoundError(f"{db} isn't built: run the build first (a failed build leaves no database)")
    ensure_private(lake)
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    repo = lake._repo_id()
    with tempfile.TemporaryDirectory() as d, duckdb.connect(str(db), read_only=True) as con:
        local = Path(d) / "importer_offers.parquet"
        con.execute(
            f"copy (select * from importer_offers order by week, importer, \"row\") to '{local}' "
            "(format parquet, compression zstd)"
        )
        row = con.execute("select count(*), count(distinct week), max(week) from importer_offers").fetchone()
        assert row is not None  # an aggregate always returns a row
        lots, weeks, latest = row
        by_importer = dict(
            con.execute("select importer, count(*) from importer_offers group by 1 order by 1").fetchall()
        )
        latest_week = latest.isoformat() if latest else None
        there = _hub(api.get_paths_info, repo, [DERIVED], repo_type="dataset")
        sent = not (there and _same_file(there[0], local))
        if sent:
            _hub(
                api.upload_file,
                path_or_fileobj=str(local),
                path_in_repo=DERIVED,
                repo_id=repo,
                repo_type="dataset",
                commit_message=f"importer_offers: {lots} lots, {weeks} weeks through {latest_week}"
                + (f", built from {sha[:12]}" if sha else ""),
            )
    return {
        "path": DERIVED,
        "lots": lots,
        "lots_by_importer": by_importer,
        "weeks": weeks,
        "latest_week": latest_week,
        "sent": sent,
    }


# --- the weekly job -----------------------------------------------------------------------------------------------

CARD = "importers_card.md"  # the dataset card, shipped in the package


def week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def path(importer: str, week: date) -> str:
    return f"raw/week={week.isoformat()}/importer={importer}.parquet"


def _transient(e: BaseException) -> bool:
    """A Hub failure worth retrying: a dropped connection, a timeout, a 429 or a 5xx. Never a 4xx, a missing repo or
    a refused permission."""
    response = getattr(e, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code == 429 or code >= 500
    return isinstance(e, ConnectionError | TimeoutError | httpx.TransportError) or any(
        n in type(e).__name__ for n in ("Connection", "Timeout")
    )


def _hub(fn: Callable, *args, **kwargs):
    """A Hub call this job can't go on without, retried after HUB_RETRY_WAITS on a transient failure."""
    for wait in (*HUB_RETRY_WAITS, None):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if wait is None or not _transient(e):
                raise
            log.warning("Hub call failed (%s: %s); retrying in %.0fs", type(e).__name__, str(e)[:200], wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _repo(url: str) -> str:
    return url.removeprefix("hf://datasets/").split("@")[0].rstrip("/").casefold()


# Datasets these snapshots must never be written to, by variable, with the owner's dataset as the default where CI
# doesn't set the variable: the lake (its tables build the public release), the public export, and the app's photo
# and account datasets.
OTHER_DATASETS = {
    "COFFEEDATA_LAKE": "hf://datasets/jtouley/coffee-data",
    "PUBLIC_EXPORT_URL": "hf://datasets/jtouley/us-coffee-market",
    "COFFEEDATA_PHOTOS": "hf://datasets/jtouley/coffee-data-photos",
    "COFFEEDATA_USERS": "hf://datasets/jtouley/coffee-data-users",
}
# What an importers dataset may hold: the raw snapshots, the derived copy (derived/, DERIVED_FILES alone), the card.
# An existing dataset with anything else is someone else's: refused.
OWN_TOP_LEVEL = {"raw", "derived", "README.md", ".gitattributes"}


def ensure_private(lake: Lake) -> None:
    """Make sure the importers dataset is a private one of its own before anything is fetched or written:
    - its name is none of OTHER_DATASETS' (their variables' values and the defaults), compared case-insensitively;
    - an existing dataset holds nothing but this job's files (OWN_TOP_LEVEL, and under derived/ only
      DERIVED_FILES); a missing one is created private;
    - the Hub says it is private (`private is True`; anything else is refused);
    then send the dataset card if it is missing or changed. A local lake needs none of this."""
    if not lake.remote:
        return
    repo = lake._repo_id()
    for var, default in OTHER_DATASETS.items():
        for url in {default, os.environ.get(var) or ""}:
            if url.startswith("hf://datasets/") and _repo(url) == repo.casefold():
                raise RuntimeError(
                    f"COFFEEDATA_IMPORTERS names the same dataset as {var}; refusing to write there"
                )
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    if _hub(api.repo_exists, repo, repo_type="dataset"):
        files = _hub(api.list_repo_files, repo, repo_type="dataset")
        foreign = {f.split("/")[0] for f in files} - OWN_TOP_LEVEL
        foreign |= {f for f in files if f.startswith("derived/") and f not in DERIVED_FILES}
        if foreign:
            raise RuntimeError(
                f"{repo} holds files this job didn't write ({', '.join(sorted(foreign))}); refusing to use it"
            )
    else:
        _hub(api.create_repo, repo, repo_type="dataset", private=True, exist_ok=True)
    if _hub(api.dataset_info, repo).private is not True:
        raise RuntimeError(f"the importers dataset {repo} is not private; refusing to write to it")
    card = resources.files("coffeedata").joinpath(CARD)
    with tempfile.TemporaryDirectory() as d:
        local = Path(d) / "README.md"
        local.write_text(card.read_text())
        there = _hub(api.get_paths_info, repo, ["README.md"], repo_type="dataset")
        if not there or not _same_file(there[0], local):
            _hub(
                api.upload_file,
                path_or_fileobj=str(local),
                path_in_repo="README.md",
                repo_id=repo,
                repo_type="dataset",
                commit_message="dataset card",
            )


def _weeks(lake: Lake, importer: str) -> list[str]:
    """The paths of this importer's stored weeks, in the dataset and in staging, oldest first."""
    suffix = f"/importer={importer}.parquet"
    local = [
        f.relative_to(lake.root).as_posix() for f in lake.root.glob(f"raw/week=*/importer={importer}.parquet")
    ]
    remote: list[str] = []
    if lake.remote:
        from huggingface_hub import HfApi

        api = HfApi(token=os.environ.get("HF_TOKEN"))
        remote = [
            f for f in _hub(api.list_repo_files, lake._repo_id(), repo_type="dataset") if f.endswith(suffix)
        ]
    return sorted({p for p in [*local, *remote] if p.startswith("raw/week=") and "0000-00-00" not in p})


def stored(lake: Lake, rel: str) -> bool:
    """Whether this importer's week is already in the dataset (or staged, from a publish that failed)."""
    if (lake.root / rel).exists():
        return True
    if not lake.remote:
        return False
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    return bool(_hub(api.get_paths_info, lake._repo_id(), [rel], repo_type="dataset"))


def last_count(lake: Lake, importer: str, week: date) -> int | None:
    """How many lots the importer's last stored week before `week` had; None when there is none."""
    before = [p for p in _weeks(lake, importer) if p < path(importer, week)]
    if not before:
        return None
    rel = before[-1]
    if (lake.root / rel).exists():
        return pq.read_metadata(lake.root / rel).num_rows
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    with tempfile.TemporaryDirectory() as d:
        local = _hub(api.hf_hub_download, lake._repo_id(), rel, repo_type="dataset", local_dir=d)
        return pq.read_metadata(local).num_rows


def fetch(client: PoliteClient, url: str) -> str:
    """The page's HTML, or FetchError: a block, a robots.txt disallow, a challenge or any status but 200."""
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__} {url}") from e
    if err := challenge_error(r, url):
        raise err
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code} {url}", status=r.status_code)
    return r.text


def snapshot(
    lake: Lake,
    client: PoliteClient,
    names: list[str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    accept_drop: bool = False,
) -> list[dict]:
    """Fetch each importer's page and store it raw for this week (the week of the fetch), publishing each file as it
    is written.

    One result per importer: `stored` (with its row and null-cell counts), `skipped` (the week was already stored,
    so the page isn't fetched again) or `failed` (with the error). Every importer is tried; the caller fails the run
    when any failed. Nothing is written for a failed importer.
    """
    ensure_private(lake)
    week = week_of(now().date())
    results: list[dict] = []
    for name in names or list(IMPORTERS):
        imp, rel = IMPORTERS[name], path(name, week)
        try:
            url = imp.where()
            if url is None:  # Royal, on any run but a scheduled one's first attempt
                results.append(
                    {
                        "importer": name,
                        "week": str(week),
                        "status": "skipped",
                        "reason": "fetched only on a scheduled run's first attempt",
                    }
                )
                continue
            if stored(lake, rel):
                lake.publish(
                    f"importer snapshot: {name}, week of {week}"
                )  # a staged file from a failed publish
                results.append({"importer": name, "week": str(week), "status": "skipped"})
                continue
            fetched_at = now().replace(microsecond=0)
            last = last_count(lake, name, week)
            parsed = imp.read(client, url, last) if imp.read else parse(name, fetch(client, url))
            extra: dict = {}
            accept = accept_drop
            if name == ROYAL:
                royal_ok, note = royal_accepts_drop(week)
                accept = accept or royal_ok
                extra = {"price_fields_dropped": parsed.dropped_prices} | ({"note": note} if note else {})
            thinned = bool(last and len(parsed.lots) < MIN_SHARE_OF_LAST_WEEK * last)
            if thinned and not accept:
                how = (
                    f"if the drop is real, set the repository variable {ROYAL_ACCEPT_DROP_VAR}={week.isoformat()}; "
                    "the next scheduled run (next week) accepts it"
                    if name == ROYAL
                    else "if the drop is real, run again with --accept-drop"
                )
                raise LayoutChanged(
                    f"{name}: {len(parsed.lots)} lots, under {MIN_SHARE_OF_LAST_WEEK:.0%} of the {last} in its last "
                    f"stored week (a thinned page?); {how}"
                )
            if thinned:
                extra["accepted_drop"] = True
            base = {
                "week": week,
                "fetched_at": fetched_at,
                "importer": name,
                "source_url": url,
                "parser_version": PARSER_VERSION,
            }
            rows = [
                base
                | {
                    "row": lot.row,
                    "url": lot.url,
                    "cells": [{"column": c, "text": v} for c, v in lot.cells.items()],
                }
                for lot in parsed.lots
            ]
            lake.write("importer_raw", rel, rows)
            lake.publish(f"importer snapshot: {name}, week of {week}")
            results.append(
                {
                    "importer": name,
                    "week": str(week),
                    "status": "stored",
                    "rows": len(rows),
                    "unreadable_rows": parsed.unreadable_rows,
                    "null_cells": parsed.bad_cells,
                    "last_week_rows": last,
                }
                | extra
            )
        except Exception as e:  # noqa: BLE001 - reported per importer; the caller fails the run
            log.error("%s: %s: %s", name, type(e).__name__, e)
            results.append(
                {
                    "importer": name,
                    "week": str(week),
                    "status": "failed",
                    "error": f"{type(e).__name__}: {e}"[:500],
                }
            )
    return results
