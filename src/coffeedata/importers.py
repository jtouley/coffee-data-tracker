"""Weekly private snapshots of green-coffee importers' public offer lists.

Two importers, two public pages, fetched in one pass a week, never logged in, with the crawler's client
(http.PoliteClient: our user agent, robots.txt honored, bounded retries with backoff and jitter, Retry-After
respected):

- Cafe Imports, https://www.cafeimports.com/north-america/offerings (a server-rendered table; prices need a login
  and aren't on it): lot ID, name, origin, region, process, status (Spot / Afloat / At origin), estimated arrival,
  bags and bag size. No warehouse.
- InterAmerican, https://interamericancoffee.com/our-offerings/ (a wpDataTables table): warehouse, product name,
  bags, bag weight, status (AFLOAT / IN STORE). No lot ID, origin, region, process or arrival column, so those
  stated fields stay empty.

- Royal Coffee, a Shopify store: its public /products.json listing, 250 products a page, the pages spaced
  ROYAL_PAGE_INTERVAL apart. Raw-only (below), and fetched only by the scheduled workflow: see royal_listing_url.

Two layers, each one immutable file per importer per week, in a private Hugging Face dataset of its own
(`COFFEEDATA_IMPORTERS`, created private by `ensure_private` with the existing credential):
- importer_raw (`raw/`): each lot word for word, as the roaster adapters keep a product's whole text: every cell of
  its row under the page's header, and its own page's URL (only cells about prices are left out), so the normalized
  values and anything derived later can be derived again from it;
- importer_offers (`offers/`): the fields the page states, and our model's values derived from the raw cells by
  deterministic rules that abstain when unsure (importer_rules.py). A raw-only importer (Royal) has none: its
  values are derived from the raw layer when it is read (derive), so a better rule re-derives every week.
No prices, page HTML or images. Backend-only, for internal use and a future roaster portal (which may show only the
normalized values, never the importer's wording): dbt never reads it, and nothing in the public tables, the release,
the site or the public dataset does (tests/test_importers.py). A week already stored is never fetched again.

A changed page fails loudly (LayoutChanged): a missing table or column, zero rows, or more than MAX_BAD_SHARE of
rows that can't be read is an error, never an empty or thinned snapshot. The importer's week is then left unwritten,
so a re-run that week (after the parser is fixed) fetches it again.

What these snapshots are for (arrival-to-roaster lag, after a year or more of history): the green-importer
feasibility check, recommendation 2. .github/workflows/importers.yml runs it each week.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .http import FetchError, PoliteClient, challenge_error
from .lake import Lake, _same_file

log = logging.getLogger(__name__)

PARSER_VERSION = "1"
# A page where more than this share of table rows can't be read has changed under the parser: fail, don't thin.
MAX_BAD_SHARE = 0.10
# Waits between tries of a Hub call this job can't do without (is this week already stored? is the dataset private?).
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


class Record(dict[str, list[str]]):
    """One table row as {column (casefolded): cell lines}, with every cell under its header as the page names it
    (`cells`, in page order) and the row's link targets (`links`)."""

    cells: list[tuple[str, list[str]]]
    links: list[str]


def records(table: Table) -> tuple[list[Record], int]:
    """The table's rows, and how many rows had the wrong number of cells (left out)."""
    names = [h or f"column {i + 1}" for i, h in enumerate(table.header)]  # a header cell with no text
    out, bad = [], 0
    for k, row in enumerate(table.rows):
        if len(row) != len(names):
            bad += 1
            continue
        rec = Record(zip((n.casefold() for n in names), row, strict=True))
        rec.cells = list(zip(names, row, strict=True))
        rec.links = table.links[k] if k < len(table.links) else []
        out.append(rec)
    return out, bad


def checked(importer: str, offers: list[dict], bad: int) -> list[dict]:
    """The parsed offers, or LayoutChanged when there are none or too many rows couldn't be read."""
    total = len(offers) + bad
    if not offers:
        raise LayoutChanged(f"{importer}: zero rows parsed ({bad} unreadable)")
    if bad / total > MAX_BAD_SHARE:
        raise LayoutChanged(f"{importer}: {bad} of {total} rows unreadable (more than {MAX_BAD_SHARE:.0%})")
    if bad:
        log.warning("%s: %d of %d rows unreadable, left out", importer, bad, total)
    return offers


# --- field readers ------------------------------------------------------------------------------------------------

STATUSES = {"spot": "spot", "afloat": "afloat", "at origin": "at_origin", "in store": "in_store"}
LB_KG = 0.45359237


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


def _first(cell: list[str]) -> str:
    return cell[0] if cell else ""


def _whole(cell: list[str]) -> str | None:
    return _clean(" ".join(cell)) or None


def _verbatim(cell: list[str]) -> str | None:
    """A cell's text as the page shows it, line by line (whitespace runs collapsed, as a browser does)."""
    return "\n".join(cell) or None


# A cell about prices ("Log In For Price", "$4.10/lb"): never stored, in either layer.
PRICE = re.compile(r"\bprices?\b|\$\s*\d", re.IGNORECASE)


def raw_cells(row: Record) -> dict[str, str | None]:
    """Every cell of the row, word for word under its header, as the roaster adapters keep a product's whole text:
    nothing trimmed or summarized, so everything derived later can be derived again from it. The only cells left out
    are about prices; text the page hides from readers (`class="hide"` copies), markup, links and images were never
    cell text."""
    return {name: _verbatim(lines) for name, lines in row.cells if not PRICE.search(" ".join(lines))}


def lot_url(row: Record, page: str) -> str | None:
    """The lot's own page on the importer's site: the row's first link on the same host, resolved against the
    offer page (never a login or portal link)."""
    host = urlsplit(page).hostname
    for href in row.links:
        url = urljoin(page, href)
        parts = urlsplit(url)
        if (
            parts.scheme in ("http", "https")
            and parts.hostname == host
            and not re.search(r"login|portal", url, re.I)
        ):
            return url
    return None


def _int(cell: list[str]) -> int | None:
    t = _whole(cell) or ""
    return int(t.replace(",", "")) if re.fullmatch(r"\d[\d,]*", t) else None


# --- the two pages ------------------------------------------------------------------------------------------------

CAFE_IMPORTS_COLUMNS = (
    "Origin",
    "Name",
    "ID",
    "Bag Size",
    "Bags",
    "Location",
    "Est Arrival",
    "Region",
    "Process",
)


LOT_ID = re.compile(r"\d+(?:-\d+)?")
CAFE_IMPORTS_URL = "https://www.cafeimports.com/north-america/offerings"
INTERAMERICAN_URL = "https://interamericancoffee.com/our-offerings/"


def parse_cafe_imports(html: str) -> list[dict]:
    """Cafe Imports' North America offerings table (id offering-table; the archive table beside it is filled in the
    browser, so it is empty here and not read)."""
    rows, bad = records(find_table(html, CAFE_IMPORTS_COLUMNS))
    offers = []
    for i, r in enumerate(rows):
        lot, name, bags = _whole(r["id"]), _whole(r["name"]), _int(r["bags"])
        # a lot ID is a number, and a lot split for another shipment carries a suffix: "25748-2"
        if not lot or not LOT_ID.fullmatch(lot) or not name or bags is None:
            bad += 1
            continue
        # "Afloat / Est Arrival: Early Jan 2027" -> "Early Jan 2027"
        arrival = re.sub(r"^.*?est\.? arrival:?\s*", "", _whole(r["est arrival"]) or "", flags=re.IGNORECASE)
        offers.append(
            {
                "lot_id": lot,
                "name": name,
                "origin": _whole(r["origin"]),
                "region": _whole(r["region"]),
                "process": _whole(r["process"]),
                "status": status(_first(r["location"])),
                "est_arrival": arrival or None,
                "warehouse": None,
                "bags": bags,
                "bag_size": _whole(r["bag size"]),
                "bag_kg": bag_kg(_whole(r["bag size"]) or ""),
                "row": i,
                "_raw": raw_cells(r),
                "_url": lot_url(r, CAFE_IMPORTS_URL),
            }
        )
    return checked("cafe_imports", offers, bad)


INTERAMERICAN_COLUMNS = ("Location/Destination", "Product", "Available Bags", "Bag Weight", "Status")


def parse_interamerican(html: str) -> list[dict]:
    """InterAmerican's offerings table (a wpDataTables table, rendered on the server)."""
    rows, bad = records(find_table(html, INTERAMERICAN_COLUMNS))
    offers = []
    for i, r in enumerate(rows):
        name, bags = _whole(r["product"]), _int(r["available bags"])
        if not name or bags is None:
            bad += 1
            continue
        offers.append(
            {
                "lot_id": None,
                "name": name,
                "origin": None,
                "region": None,
                "process": None,
                "status": status(_whole(r["status"]) or ""),
                "est_arrival": None,
                "warehouse": _whole(r["location/destination"]),
                "bags": bags,
                "bag_size": _whole(r["bag weight"]),
                "bag_kg": bag_kg(_whole(r["bag weight"]) or ""),
                "row": i,
                "_raw": raw_cells(r),
                "_url": lot_url(r, INTERAMERICAN_URL),
            }
        )
    return checked("interamerican", offers, bad)


# --- Royal Coffee: a Shopify storefront's public product listing ---------------------------------------------------
#
# Royal Coffee's terms restrict automated access, single out AI tools, and restrict storing content; the owner chose
# to include it and accepts that risk. To respect the terms' AI clause, no AI agent ever fetches the site: only the
# scheduled importers workflow, a plain program, does, once a week. So the store's address is not in this code at all.
# The workflow passes it in ROYAL_URL_VAR on scheduled runs only, and royal_listing_url() refuses it anywhere else
# (under pytest, outside GitHub Actions, in another workflow, or on a dispatch). Tests use invented products and a
# made-up host (tests/fixtures/importers/royal_synthetic_products.json).

ROYAL = "royal_coffee"
ROYAL_URL_VAR = "COFFEEDATA_ROYAL_URL"
# The importers workflow's name, as GITHUB_WORKFLOW reports it.
ROYAL_WORKFLOW = "importers"
# The most products a /products.json page holds (adapters.shopify.PAGE_LIMIT, from Shopify's REST pagination docs), so
# the listing takes the fewest pages.
ROYAL_PAGE_LIMIT = 250
# A listing that needs more pages than this has changed under us (or pages forever): fail, don't keep asking.
ROYAL_MAX_PAGES = 20
# Seconds between two requests to the store (the client's per-host spacing; a longer robots.txt Crawl-delay wins).
ROYAL_PAGE_INTERVAL = 15.0


class NotAllowedHere(RuntimeError):
    """Royal's address was given to something other than the scheduled importers workflow."""


def royal_listing_url(env: dict[str, str] | None = None) -> str | None:
    """Royal's /products.json URL on the scheduled importers workflow's run, else None (the importer is skipped).
    NotAllowedHere when the address is set anywhere else: under pytest, outside GitHub Actions, in another workflow
    or on a run that wasn't scheduled. Nothing else in the code knows the address."""
    import sys

    env = dict(os.environ) if env is None else env
    base = (env.get(ROYAL_URL_VAR) or "").strip()
    if not base:
        return None
    if (
        env.get("GITHUB_ACTIONS") != "true"
        or env.get("GITHUB_WORKFLOW") != ROYAL_WORKFLOW
        or env.get("GITHUB_EVENT_NAME") != "schedule"
    ):
        raise NotAllowedHere(f"{ROYAL_URL_VAR} is read only on the scheduled {ROYAL_WORKFLOW} workflow")
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        raise NotAllowedHere(f"{ROYAL_URL_VAR} is set under pytest; tests never reach Royal's site")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname or parts.path.strip("/") or parts.query:
        raise NotAllowedHere(f"{ROYAL_URL_VAR} must be a bare https:// origin")
    return f"https://{parts.hostname}/products.json"


def read_royal(client: PoliteClient, url: str) -> list[dict]:
    """Every product in the store's public listing, one page after another at ROYAL_PAGE_LIMIT a page, with the
    crawler's client (our user agent, robots.txt honored, per-host spacing, bounded retries; never logged in). Stops
    at the first page shorter than the limit (or empty, or repeating one already read), so a listing of N products
    takes ceil(N / 250) requests, plus one when N is a multiple of 250. Then parse_royal."""
    products: dict[str, dict] = {}
    for page in range(1, ROYAL_MAX_PAGES + 1):
        data = client.fetch_json(url, limit=ROYAL_PAGE_LIMIT, page=page)
        if not isinstance(data, dict) or not isinstance(data.get("products"), list):
            raise LayoutChanged(f"{ROYAL}: page {page} is not a /products.json listing")
        batch = [p for p in data["products"] if isinstance(p, dict)]
        new = {str(p.get("id")): p for p in batch if str(p.get("id")) not in products}
        products |= new
        if not new or len(data["products"]) < ROYAL_PAGE_LIMIT:
            return parse_royal(list(products.values()), url)
    raise LayoutChanged(f"{ROYAL}: more than {ROYAL_MAX_PAGES} pages of {ROYAL_PAGE_LIMIT}; not stored")


# The product fields kept, by name (never by what their text says): anything else the listing carries, prices and
# images included, is never stored. As the roaster adapters keep a Shopify product (snapshots and bodies), with the
# body's text from body_html (text.html_to_text: its words, without markup or image tags).
ROYAL_PRODUCT_FIELDS = ("id", "title", "handle", "vendor", "product_type", "tags", "published_at")
ROYAL_VARIANT_FIELDS = ("id", "title", "option1", "option2", "option3", "sku", "available", "grams")
ROYAL_OPTION_FIELDS = ("name", "values")


def royal_cells(p: dict) -> dict[str, str | None]:
    """One product word for word, as {field: text}: each kept field's value (a list of tags one per line; options and
    variants as JSON with only their kept fields), and body_html's text. Null where the store gives nothing."""
    import json

    from .text import html_to_text

    def text(v: object) -> str | None:
        if v is None or v == "" or v == []:
            return None
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return "\n".join(v)
        if isinstance(v, (list, dict)):
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


def parse_royal(products: list[dict], url: str) -> list[dict]:
    """The listing's products as raw rows. A product without an id, title or handle can't be read (counted, and more
    than MAX_BAD_SHARE of them is LayoutChanged, as is an empty listing)."""
    origin = f"{urlsplit(url).scheme}://{urlsplit(url).hostname}"
    offers: list[dict] = []
    bad = 0
    for p in products:
        if not p.get("id") or not str(p.get("title") or "").strip() or not p.get("handle"):
            bad += 1
            continue
        offers.append(
            {
                "lot_id": None,
                "row": len(offers),
                "_raw": royal_cells(p),
                "_url": f"{origin}/products/{p['handle']}",
            }
        )
    return checked(ROYAL, offers, bad)


@dataclass(frozen=True)
class Importer:
    url: str | None
    parse: Callable[[str], list[dict]] | None = None
    # A source that isn't one HTML page: where it is this run (None: skip it) and how to read it.
    locate: Callable[[], str | None] | None = None
    read: Callable[[PoliteClient, str], list[dict]] | None = None
    # Only the raw layer is stored; our values are derived from it when read (derive()).
    raw_only: bool = False

    def where(self) -> str | None:
        return self.locate() if self.locate else self.url

    def get(self, client: PoliteClient, url: str) -> list[dict]:
        if self.read:
            return self.read(client, url)
        assert self.parse is not None
        return self.parse(fetch(client, url))


IMPORTERS = {
    "cafe_imports": Importer(CAFE_IMPORTS_URL, parse_cafe_imports),
    "interamerican": Importer(INTERAMERICAN_URL, parse_interamerican),
    ROYAL: Importer(None, locate=royal_listing_url, read=read_royal, raw_only=True),
}


CARD = Path("docs/IMPORTERS_DATASET_CARD.md")


# --- the weekly job -----------------------------------------------------------------------------------------------


def week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


LAYERS = {"importer_raw": "raw", "importer_offers": "offers"}


def path(importer: str, week: date, table: str = "importer_offers") -> str:
    return f"{LAYERS[table]}/week={week.isoformat()}/importer={importer}.parquet"


def split(
    importer: str, parsed: list[dict], base: dict, offers_too: bool = True
) -> tuple[list[dict], list[dict]]:
    """The parser's rows as (importer_raw rows, importer_offers rows): the raw cells verbatim, and the stated fields
    with our normalized values derived from those cells (none for a raw-only importer: `offers_too` False)."""
    from .importer_rules import normalize

    raw, offers = [], []
    for p in parsed:
        cells = p["_raw"]
        stated = {k: v for k, v in p.items() if not k.startswith("_")}
        raw.append(
            base
            | {
                "row": p["row"],
                "lot_id": p["lot_id"],
                "url": p["_url"],
                "cells": [{"column": c, "text": v} for c, v in cells.items()],
            }
        )
        if offers_too:
            offers.append(base | stated | normalize(importer, cells, origin=p["origin"]))
    return raw, offers


def derive(raw_row: dict) -> dict:
    """Our values for one importer_raw row, derived when it is read (importer_rules.normalize): nothing derived is
    stored for a raw-only importer, so a better rule re-derives every week."""
    from .importer_rules import normalize

    cells = {c["column"]: c["text"] for c in raw_row["cells"]}
    keep = ("week", "fetched_at", "importer", "source_url", "row", "url")
    return {k: raw_row.get(k) for k in keep} | normalize(raw_row["importer"], cells)


def _hub(fn: Callable, *args, **kwargs):
    """A Hub call this job can't go on without, retried after HUB_RETRY_WAITS; the last failure is raised."""
    for wait in (*HUB_RETRY_WAITS, None):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if wait is None:
                raise
            log.warning("Hub call failed (%s: %s); retrying in %.0fs", type(e).__name__, str(e)[:200], wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _repo(url: str) -> str:
    return url.removeprefix("hf://datasets/").split("@")[0].rstrip("/")


# Datasets these snapshots must never be written to: the lake (its tables build the public release), the public
# export, and the app's photo and account datasets.
OTHER_DATASETS = ("COFFEEDATA_LAKE", "PUBLIC_EXPORT_URL", "COFFEEDATA_PHOTOS", "COFFEEDATA_USERS")


def ensure_private(lake: Lake) -> None:
    """Make sure the importers dataset is a private one of its own before anything is fetched or written: refuse
    another dataset's URL, create it private if it doesn't exist yet (with HF_TOKEN), and refuse it if it is
    public. A local lake needs none of this."""
    if not lake.remote:
        return
    repo = _repo(lake.url)
    for var in OTHER_DATASETS:
        other = os.environ.get(var) or ""
        if other.startswith("hf://datasets/") and _repo(other) == repo:
            raise RuntimeError(
                f"COFFEEDATA_IMPORTERS names the same dataset as {var}; refusing to write there"
            )
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    _hub(api.create_repo, repo, repo_type="dataset", private=True, exist_ok=True)
    info = _hub(api.dataset_info, repo)
    if info.private is not True:
        raise RuntimeError(f"the importers dataset {repo} is not private; refusing to write to it")
    # The dataset card says what this is and that it is backend-only; sent when missing or changed.
    there = _hub(api.get_paths_info, repo, ["README.md"], repo_type="dataset")
    if not there or not _same_file(there[0], CARD):
        _hub(
            api.upload_file,
            path_or_fileobj=str(CARD),
            path_in_repo="README.md",
            repo_id=repo,
            repo_type="dataset",
            commit_message="dataset card",
        )


def stored(lake: Lake, rel: str) -> bool:
    """Whether this importer's week is already in the dataset (or staged, from a publish that failed)."""
    if (lake.root / rel).exists():
        return True
    if not lake.remote:
        return False
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    return bool(_hub(api.get_paths_info, _repo(lake.url), [rel], repo_type="dataset"))


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
    today: date | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[dict]:
    """Fetch, parse and store each importer's page for the week of `today`, publishing each as it is written.

    One result per importer: `stored` (with its row count), `skipped` (the week was already stored, so the page
    isn't fetched again) or `failed` (with the error). Every importer is tried; the caller fails the run when any
    failed. Nothing is written for a failed importer.
    """
    week = week_of(today or now().date())
    results: list[dict] = []
    ensure_private(lake)
    for name in names or list(IMPORTERS):
        imp = IMPORTERS[name]
        # the file that says the week is stored: the offers file, or the raw file of a raw-only importer
        rel = path(name, week, "importer_raw" if imp.raw_only else "importer_offers")
        try:
            url = imp.where()
            if url is None:  # Royal, on any run but the scheduled one
                results.append(
                    {
                        "importer": name,
                        "week": str(week),
                        "status": "skipped",
                        "reason": "fetched on schedule only",
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
            parsed = imp.get(client, url)
            base = {
                "week": week,
                "fetched_at": fetched_at,
                "importer": name,
                "source_url": url,
                "parser_version": PARSER_VERSION,
            }
            raw, offers = split(name, parsed, base, offers_too=not imp.raw_only)
            # a raw file staged without its offers file is a write that died before finishing: never published
            (lake.root / path(name, week, "importer_raw")).unlink(missing_ok=True)
            # both layers in one commit: a week has both or neither (a raw-only importer: its raw file)
            lake.write("importer_raw", path(name, week, "importer_raw"), raw)
            if not imp.raw_only:
                lake.write("importer_offers", rel, offers)
            lake.publish(f"importer snapshot: {name}, week of {week}")
            results.append({"importer": name, "week": str(week), "status": "stored", "rows": len(raw)})
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
