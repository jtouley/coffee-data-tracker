"""Weekly private snapshots of green-coffee importers' public offer lists (ELT: the snapshot is raw only).

Two importers, two public pages, fetched in one pass a week, never logged in, with the crawler's client
(http.PoliteClient: our user agent, robots.txt honored, bounded retries with backoff and jitter, Retry-After
respected):

- Cafe Imports, https://www.cafeimports.com/north-america/offerings (a server-rendered table; prices need a login
  and aren't on it): lot ID, name, origin, region, process, status (Spot / Afloat / At origin), estimated arrival,
  bags and bag size. No warehouse.
- InterAmerican, https://interamericancoffee.com/our-offerings/ (a wpDataTables table): warehouse, product name,
  bags, bag weight, status (AFLOAT / IN STORE). No lot ID, origin, region, process or arrival column.

Stored (load): one immutable file per importer per week, `raw/week=<Monday>/importer=<name>.parquet`
(importer_raw), in a private Hugging Face dataset of its own (`COFFEEDATA_IMPORTERS`, created private by
`ensure_private` with the existing credential). Each lot is kept word for word, as the roaster adapters keep a
product: every cell of its row under the page's header, and the lot's own page URL. Columns about prices are left
out by their header (a header-less column, by what all its cells say), and a currency amount in any kept cell fails
the run, so no price is ever stored. No page HTML or images either.

Derived (transform), never stored: `offers()` reads the whole raw history and derives each lot's stated fields
(`stated`) and our model's normalized values (importer_rules.normalize) at read time, so a better rule re-derives
every week. Those rules abstain when unsure and aren't evaluated yet (CLAUDE.md): nothing relies on them.

Backend-only, for internal use and a future roaster portal (which may show only the normalized values, never the
importer's wording): dbt never reads the dataset, and nothing in the public tables, the release, the site or the
public dataset does (tests/test_importers.py). A week already stored is never fetched again.

A changed page fails loudly (LayoutChanged), and that importer's week stays unwritten so a run later that week
fetches it again:
- a missing table or column, zero rows, or more than MAX_BAD_SHARE of rows without their identity (lot ID, name);
- a currency amount in a stored cell;
- fewer than MIN_SHARE_OF_LAST_WEEK of the rows the last stored week had (a thinned page), unless the run is told to
  accept the drop (`--accept-drop`, the workflow's `accept_drop` input).
A single cell that can't be read (bags that aren't a number, a rule that fails on odd text) is null in the derived
view and counted; it never fails the week.

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
PRICE_HEADER = re.compile(r"\bprices?\b|\bcosts?\b|\$|/\s*lb\b", re.IGNORECASE)
# A header-less column whose every non-empty cell is about prices ("Log In For Price") is a price column too.
PRICE_TEXT = re.compile(r"\bprices?\b|\bpricing\b", re.IGNORECASE)
# A currency amount: in a stored cell it means a price got past the column filter, so the run fails.
CURRENCY = re.compile(r"[$€£]\s*\d|\bUSD\s*\d|\d\s*(?:USD|usd)\b")


def _verbatim(lines: list[str]) -> str | None:
    """A cell's text as the page shows it, line by line (whitespace runs collapsed, as a browser does)."""
    return "\n".join(lines) or None


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
    names = [h or f"column {i + 1}" for i, h in enumerate(table.header)]  # a header cell with no text
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


def stated(importer: str, cells: dict[str, str | None], problems: list[str] | None = None) -> dict:
    """The fields the page states for one lot, read from its stored cells. A cell that can't be read is None and
    noted in `problems`; `lot_id` and `name` are None when the row lacks its identity."""
    problems = [] if problems is None else problems
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


def parse(importer: str, html: str) -> Parsed:
    """The page's lots, checked: LayoutChanged on zero rows or more than MAX_BAD_SHARE without their identity."""
    imp = IMPORTERS[importer]
    found, bad = lots(html, imp.columns, imp.url)
    bad_cells = 0
    for lot in found:
        problems: list[str] = []
        if not identified(importer, stated(importer, lot.cells, problems)):
            bad += 1
        bad_cells += len(problems)
    total = len(found) + bad
    if not found or len(found) == bad:
        raise LayoutChanged(f"{importer}: zero rows parsed ({bad} unreadable)")
    if bad / total > MAX_BAD_SHARE:
        raise LayoutChanged(f"{importer}: {bad} of {total} rows unreadable (more than {MAX_BAD_SHARE:.0%})")
    if bad or bad_cells:
        log.warning("%s: %d of %d rows unreadable, %d cells null", importer, bad, total, bad_cells)
    return Parsed(found, bad, bad_cells)


@dataclass(frozen=True)
class Importer:
    url: str
    columns: tuple[str, ...]  # the columns its table must have


IMPORTERS = {
    "cafe_imports": Importer(
        "https://www.cafeimports.com/north-america/offerings",
        ("Origin", "Name", "ID", "Bag Size", "Bags", "Location", "Est Arrival", "Region", "Process"),
    ),
    "interamerican": Importer(
        "https://interamericancoffee.com/our-offerings/",
        ("Location/Destination", "Product", "Available Bags", "Bag Weight", "Status"),
    ),
}


# --- the read-time transform --------------------------------------------------------------------------------------


def derive(raw: dict) -> dict:
    """One stored lot (an importer_raw row) as an offer: its keys, the stated fields and our normalized values.
    `problems` lists what read as null."""
    from .importer_rules import normalize

    cells = {c["column"]: c["text"] for c in raw["cells"]}
    problems: list[str] = []
    keys = {k: raw[k] for k in ("week", "fetched_at", "importer", "row", "url")}
    fields = stated(raw["importer"], cells, problems)
    return keys | fields | normalize(raw["importer"], cells, problems) | {"problems": problems}


def offers(lake: Lake) -> list[dict]:
    """Every stored lot, every week, derived now (`derive`): rebuilt from raw on each read, never stored."""
    rel = lake.connect().sql(
        f"select * from {lake.scan('importer_raw')} where importer is not null order by week, importer, row"
    )
    return [derive(dict(zip(rel.columns, r, strict=True))) for r in rel.fetchall()]


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
# What an importers dataset may hold. An existing dataset with anything else is someone else's: refused.
OWN_TOP_LEVEL = {"raw", "README.md", ".gitattributes"}


def ensure_private(lake: Lake) -> None:
    """Make sure the importers dataset is a private one of its own before anything is fetched or written:
    - its name is none of OTHER_DATASETS' (their variables' values and the defaults), compared case-insensitively;
    - an existing dataset holds nothing but this job's files (OWN_TOP_LEVEL); a missing one is created private;
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
        if foreign := sorted({f.split("/")[0] for f in files} - OWN_TOP_LEVEL):
            raise RuntimeError(
                f"{repo} holds files this job didn't write ({', '.join(foreign)}); refusing to use it"
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
            if stored(lake, rel):
                lake.publish(
                    f"importer snapshot: {name}, week of {week}"
                )  # a staged file from a failed publish
                results.append({"importer": name, "week": str(week), "status": "skipped"})
                continue
            fetched_at = now().replace(microsecond=0)
            parsed = parse(name, fetch(client, imp.url))
            last = last_count(lake, name, week)
            if last and len(parsed.lots) < MIN_SHARE_OF_LAST_WEEK * last and not accept_drop:
                raise LayoutChanged(
                    f"{name}: {len(parsed.lots)} lots, under {MIN_SHARE_OF_LAST_WEEK:.0%} of the {last} in its last "
                    "stored week (a thinned page?); if the drop is real, run again with --accept-drop"
                )
            base = {
                "week": week,
                "fetched_at": fetched_at,
                "importer": name,
                "source_url": imp.url,
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
