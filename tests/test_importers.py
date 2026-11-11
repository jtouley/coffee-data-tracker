"""Weekly private importer snapshots (coffeedata.importers, importer_rules): the parsers on small saved pages, the
raw layer word for word with no prices, loud failure on a changed or thinned page, the read-time transform, one
immutable raw file per importer and week, the private dataset and its guards, and that nothing can reach anything
public."""

from __future__ import annotations

import re
import types
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
import yaml

from coffeedata import importer_rules as rules
from coffeedata import importers
from coffeedata.http import PoliteClient
from coffeedata.lake import GLOBS, IMPORTER_TABLES, LAKE_TABLES, SCHEMAS, Lake

FIXTURES = Path(__file__).parent / "fixtures" / "importers"
CI_HTML = (FIXTURES / "cafeimports.html").read_text()
IA_HTML = (FIXTURES / "interamerican.html").read_text()
NOW = datetime(2026, 11, 11, 17, 37, 5, 123, tzinfo=UTC)  # a Wednesday
MON = date(2026, 11, 9)  # its week
NEXT = datetime(2026, 11, 18, 17, 37, tzinfo=UTC)  # the next week


def _offers(name: str, html: str) -> list[dict]:
    """The page's lots as the read-time transform derives them."""
    p = importers.parse(name, html)
    base = {"week": MON, "fetched_at": NOW, "importer": name}
    return [
        importers.derive(base | {"row": lot.row, "url": lot.url, "cells": _cells(lot.cells)})
        for lot in p.lots
    ]


def _cells(cells: dict) -> list[dict]:
    return [{"column": c, "text": v} for c, v in cells.items()]


def _rows(html: str, keep: int) -> str:
    """The page with only its first `keep` lots."""
    rows = re.findall(
        r"<tr id=\"table_10_row_\d+\">.*?</tr>|<tr>\s*<td data-rel=\"origin\".*?</tr>", html, re.S
    )
    for r in rows[keep:]:
        html = html.replace(r, "", 1)
    return html


# --- parsing and the stated fields ---------------------------------------------------------------------------------


def test_cafe_imports_lots():
    by_lot = {o["lot_id"]: o for o in _offers("cafe_imports", CI_HTML)}
    assert list(by_lot) == ["25973", "26784", "26453", "25748-2"]
    lot = by_lot["25973"]
    assert {
        k: lot[k] for k in ("name", "origin", "region", "process", "status", "bags", "bag_size", "bag_kg")
    } == {
        "name": 'Asociación De Caficultores De Taipiplaya "ASOCAFE" (GrainPro)',
        "origin": "Bolivia",
        "region": "La Paz",
        "process": "Washed",
        "status": "spot",
        "bags": 64,
        "bag_size": "69 Kg",
        "bag_kg": 69.0,
    }
    assert lot["warehouse"] is None and lot["est_arrival"] is None and lot["row"] == 0
    assert lot["url"].startswith(importers.IMPORTERS["cafe_imports"].url + "?view=beanology.view.")
    assert by_lot["26453"]["status"] == "at_origin"
    # an afloat lot: its estimated arrival as stated, without the status or label before it
    assert by_lot["25748-2"]["status"] == "afloat" and by_lot["25748-2"]["est_arrival"] == "Mid Nov 2026"
    assert by_lot["26784"]["est_arrival"] == "Early Jan 2027"


def test_interamerican_lots():
    offers = _offers("interamerican", IA_HTML)
    assert [(o["warehouse"], o["status"], o["bags"], o["bag_kg"]) for o in offers] == [
        ("Continental Terminals Annex", "afloat", 11, 59.0),
        ("Continental Terminals Annex", "in_store", 100, 60.0),
        ("Dupuy Houston", "afloat", 175, 59.0),
        ("Continental Terminals NJ", "afloat", 17, 1000.0),  # "1MT Supersacks"
    ]
    # the page has no lot, origin, region, process or arrival column
    assert all(o[k] is None for o in offers for k in ("lot_id", "origin", "region", "process", "est_arrival"))
    assert all(o["url"].startswith("https://interamericancoffee.com/") for o in offers)


def test_field_readers():
    assert importers.bag_kg("60 Kg") == 60.0
    assert importers.bag_kg("59 Kg Bags") == 59.0
    assert importers.bag_kg("1MT Supersacks") == 1000.0
    assert importers.bag_kg("30 lbs") == 13.61
    assert importers.bag_kg("box") is None
    assert importers.status("IN STORE") == "in_store"
    assert importers.status("At origin") == "at_origin"
    assert importers.status("In Transit") == "in transit"  # an unseen value is kept as written, never dropped


# --- the raw layer: word for word, no prices -----------------------------------------------------------------------


def test_raw_layer_is_every_cell_word_for_word():
    """Like the roaster adapters' raw snapshots: every cell of the lot's row under the page's header, full text, so
    everything derived can be derived again. Only the price column is left out; no markup, links or images."""
    ci = importers.find_table(CI_HTML, importers.IMPORTERS["cafe_imports"].columns)
    expected = {
        "cafe_imports": [h for h in ci.header if h],  # the header-less column is "Log In For Price"
        "interamerican": importers.find_table(IA_HTML, importers.IMPORTERS["interamerican"].columns).header,
    }
    for name, html in (("cafe_imports", CI_HTML), ("interamerican", IA_HTML)):
        for lot in importers.parse(name, html).lots:
            assert list(lot.cells) == expected[name]
            assert not any(s in (v or "") for v in lot.cells.values() for s in ("<", "$", "Log In For Price"))
    cells = importers.parse("cafe_imports", CI_HTML).lots[1].cells
    assert (
        cells["Notes"]
        == "Caramel, burnt sugar, and oolong tea flavors with juicy acidity and syrupy sweetness."
    )
    assert cells["Location"] == "Afloat\nEst Arrival: Jan 2027"
    assert cells["Est Arrival"] == "Afloat\nEst Arrival: Early Jan 2027"
    assert cells["Program"] is None  # an empty cell is kept, as null
    assert {f.name for f in SCHEMAS["importer_raw"]} == {
        "week", "fetched_at", "importer", "source_url", "parser_version", "row", "url", "cells",
    }  # fmt: skip


def _with_column(html: str, header: str, cell: str) -> str:
    """The Cafe Imports fixture with one more column, after Notes."""
    html = html.replace(
        "<th data-rel='notes' data-visible='false'>Notes</th>", f"<th>Notes</th><th>{header}</th>"
    )
    return re.sub(r"(<td>\s*[^<]*?(?:Graham|Caramel|Tons|Lots|Cocoa)[^<]*</td>)", rf"\1<td>{cell}</td>", html)


def test_prices_are_left_out_by_column_and_a_stray_amount_fails():
    # a column whose header is a price is never stored, whatever its cells say
    page = _with_column(CI_HTML, "Price", "4.25 / lb")
    assert all("Price" not in lot.cells for lot in importers.parse("cafe_imports", page).lots)
    # a Notes cell that mentions a price is kept whole: it is text, not a price
    noted = CI_HTML.replace("Caramel, burnt sugar", "Great value at this price. Caramel, burnt sugar")
    assert (
        importers.parse("cafe_imports", noted).lots[1].cells["Notes"].startswith("Great value at this price.")
    )
    # columns headed as prices by trade terms are dropped too
    for header in ("FOB", "Differential", "Diff (c/lb)"):
        page = _with_column(CI_HTML, header, "4.10")
        assert all(header not in lot.cells for lot in importers.parse("cafe_imports", page).lots)
    # a price under any other header got past the filter: fail, never store it
    for cell in ("$4.25/lb", "4,50 €", "Price: 4.25", "4.25/lb", "310 c/lb", "4.10 per kg", "USD 4.10"):
        with pytest.raises(importers.LayoutChanged, match="currency amount"):
            importers.parse("cafe_imports", _with_column(CI_HTML, "Offer", cell))
    # weights and dates are not prices
    for text in ("60 Kg", "59 Kg Bags", "1MT Supersacks", "Est Arrival: Jan 2027", "Screen 16+"):
        assert not importers.CURRENCY.search(text)


def test_a_repeated_header_keeps_every_cell():
    page = CI_HTML.replace(
        "<th data-rel='notes' data-visible='false'>Notes</th>", "<th>Notes</th><th>Notes</th>"
    )
    page = re.sub(r"(<td>\s*[^<]*?(?:Graham|Caramel|Tons|Lots|Cocoa)[^<]*</td>)", r"\1<td>second</td>", page)
    cells = importers.parse("cafe_imports", page).lots[0].cells
    assert cells["Notes"].startswith("Graham") and cells["Notes (2)"] == "second"


# --- loud failure ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html, match",
    [
        ("<html><body><p>We're updating our offerings.</p></body></html>", "no table with the columns"),
        # the table is there but rendered by the browser: zero rows is an error, never an empty snapshot
        (CI_HTML.split("<tbody>")[0] + "<tbody></tbody></table>", "zero readable rows"),
        # a renamed column
        (CI_HTML.replace(">Bags</th>", ">Quantity</th>"), "no table with the columns"),
        # most lots lost their identity (the ID column now holds something else)
        (re.sub(r'<td data-order="(\d+)">[^<]*</td>', "<td>n/a</td>", CI_HTML), "unreadable"),
    ],
)
def test_changed_page_fails_loudly(html, match):
    with pytest.raises(importers.LayoutChanged, match=match):
        importers.parse("cafe_imports", html)


def test_a_bad_cell_is_null_and_counted_not_a_failed_week():
    page = IA_HTML.replace('<td style="">11</td>', '<td style="">n/a</td>')
    parsed = importers.parse("interamerican", page)
    assert len(parsed.lots) == 4 and parsed.bad_cells == 1 and parsed.unreadable_rows == 0
    first = _offers("interamerican", page)[0]
    assert first["bags"] is None and first["problems"] == ["bags"]
    # a rule that fails on odd text abstains for that value only
    assert rules.altitude("Lot 1.2.3 m") == (None, None)
    problems: list[str] = []
    out = rules.normalize("cafe_imports", {"Name": None, "Notes": "x", "Process": "Washed"}, problems)
    assert out["process_type"] == "washed" and problems == []


def test_a_failing_rule_never_fails_the_lot(monkeypatch):
    def boom(*a):
        raise ValueError("odd text")

    boom.__name__ = "altitude"
    monkeypatch.setattr(rules, "altitude", boom)
    problems: list[str] = []
    out = rules.normalize("cafe_imports", {"Name": "Natural (GrainPro)", "Process": "Natural"}, problems)
    assert out["altitude_min_m"] is None and out["process_type"] == "natural"
    assert problems == ["altitude: ValueError"]


# --- the normalized values: decide or abstain -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "stated, name, want",
    [
        ("Natural", "Karana - Carbonic Maceration - Anaerobic - Natural", "anaerobic"),
        ("Wet-Hulled", "Sumatra Lintong", "honey"),
        ("Pulped Natural", "Fazenda X", "honey"),
        ("Semi-Washed", "Sumatra", "honey"),
        ("Washed", "Gakenke - Kayanza - Washed", "washed"),
        (None, "Ethiopia Yirgacheffe G2 Washed", "washed"),
        # abstain: decaf, a mix, a misspelling, nothing stated
        ("Decaf", "Mountain Water Process", None),
        ("Washed", "Colombia Decaf Sugarcane", None),
        (None, "Mexico Mountain Water Decaf Natural", None),
        (None, "Costa Rica Sonora Honey Natural", None),
        (None, "Brazil Cerrado 17/18 Natrual", None),
        (None, "Colombia Supremo", None),
    ],
)
def test_process(stated, name, want):
    assert rules.process(stated, name) == want


def test_other_rules_decide_or_abstain():
    # varieties, in the catalog's spelling; Java is a variety except on an Indonesian lot
    assert rules.varieties("Natural - Fazenda Santa Lucia - Yellow Bourbon (GrainPro)") == ["Yellow Bourbon"]
    assert rules.varieties("Rwanda Java Washed", "Rwanda") == ["Java"]
    assert rules.varieties("Indonesia Java G1") is None
    assert rules.varieties("Karana - Natural", "Java") is None
    assert rules.varieties("Washed (GrainPro)") is None
    # altitude only in an altitude context
    assert rules.altitude("grown at 1,700-1,900 masl") == (1700, 1900)
    assert rules.altitude("Altitude: 1500") == (1500, 1500)
    assert rules.altitude("1800 masl") == (1800, 1800)
    assert rules.altitude("Bags of 1,000 m") == (None, None)
    assert rules.altitude("Screen 16+") == (None, None)
    # harvest months only when stated, as whole month words
    assert rules.harvest_months("Harvest: Nov - Feb") == ["nov", "dec", "jan", "feb"]
    assert rules.harvest_months("Harvested December through March") == ["dec", "jan", "feb", "mar"]
    assert rules.harvest_months("This harvest declined because of rain") is None
    assert rules.harvest_months("This harvest may be smaller") is None  # a verb
    assert rules.harvest_months("Harvest: May") == ["may"]
    assert rules.harvest_months("harvested in May") == ["may"]
    assert rules.harvest_months("Harvest May - Aug") == ["may", "jun", "jul", "aug"]
    assert rules.harvest_months("Graham with mild kahlua") is None
    # grower: a part of the name with a grower word; a placeholder keeps the type but not the name
    assert rules.grower("Natural - Fazenda Santa Lucia - Yellow Bourbon (GrainPro)") == (
        "farm",
        "Fazenda Santa Lucia",
    )
    assert rules.grower("Natural - Fazenda TBC (SC Bags)") == ("farm", None)
    assert rules.grower("Gakenke - Kayanza - Washed (Ecotact)") == (None, None)
    assert rules.grower("Guatemala Huehuetenango ASDECAFE SHB EP Organic") == (None, None)


NOT_FT_ORG = (None, ["fair_trade", "organic"])


@pytest.mark.parametrize(
    "text, want",
    [
        # "nft,norg": not fair trade, not organic; anything else (Rainforest Alliance) stays unknown
        ("nft,norg", NOT_FT_ORG),
        ("nft,norg,Microlot,Microlot nft,norg,", NOT_FT_ORG),
        (
            "Fair Trade,Fairtrade,Organic,FTO Norte FTO Fair Trade,Fairtrade,Organic,FTO",
            (["organic", "fair_trade"], None),
        ),
        (
            "Indonesia Sumatra Mandheling G1 TP Fairtrade USA Organic Certified",
            (["organic", "fair_trade"], None),
        ),
        ("nft,Organic,Organic Microlot", (["organic"], ["fair_trade"])),
        # unknown
        ("nft,Organic,Organic La Paz nft,norg,", (None, None)),  # claimed and denied
        ("C.A.F.E. Practices", (None, None)),  # a program we don't model
        ("Bird Friendly", (None, None)),
        ("nft,norg,Bird Friendly", (None, None)),
        ("Colombia Popayan Supremo", (None, None)),  # silent
        ("Certified", (None, None)),
    ],
)
def test_certifications(text, want):
    assert rules.certifications(text) == want


def test_fixture_lots_normalized():
    ci = _offers("cafe_imports", CI_HTML)
    assert [(o["process_type"], o["grower_type"], o["certifications"], o["not_certified"]) for o in ci] == [
        ("washed", "cooperative", None, None),  # its cell says both "Organic" and "norg"
        ("washed", None, *NOT_FT_ORG),
        ("natural", None, *NOT_FT_ORG),
        ("honey", "micromill", *NOT_FT_ORG),
    ]
    ia = _offers("interamerican", IA_HTML)
    assert [(o["process_type"], o["certifications"], o["not_certified"]) for o in ia] == [
        ("natural", None, None),
        (None, ["organic", "fair_trade"], None),
        ("natural", None, None),
        ("natural", None, None),
    ]
    assert {o["rules_version"] for o in ci + ia} == {rules.RULES_VERSION}


# --- the weekly job -------------------------------------------------------------------------------------------------


def _client(pages: dict[str, str], calls: list[str]) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(200, text=pages[str(request.url)], headers={"content-type": "text/html"})

    return PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(handler))


def _pages(ci: str = CI_HTML, ia: str = IA_HTML) -> dict[str, str]:
    return {importers.IMPORTERS["cafe_imports"].url: ci, importers.IMPORTERS["interamerican"].url: ia}


def _local(tmp_path) -> Lake:
    return Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)


def test_snapshot_stores_raw_once_per_week_and_offers_are_derived_on_read(tmp_path):
    lake = _local(tmp_path)
    calls: list[str] = []
    with _client(_pages(), calls) as client:
        first = importers.snapshot(lake, client, now=lambda: NOW)
    assert [(r["importer"], r["status"], r.get("rows"), r.get("null_cells")) for r in first] == [
        ("cafe_imports", "stored", 4, 0),
        ("interamerican", "stored", 4, 0),
    ]
    assert len([c for c in calls if not c.endswith("/robots.txt")]) == 2
    # raw only: one file per importer and week, nothing else
    files = sorted(p.relative_to(lake.root).as_posix() for p in lake.root.rglob("*.parquet"))
    assert files == [
        "raw/week=0000-00-00/importer=schema.parquet",
        importers.path("cafe_imports", MON),
        importers.path("interamerican", MON),
    ]
    t = pq.read_table(lake.root / importers.path("cafe_imports", MON)).to_pylist()
    assert {r["week"] for r in t} == {MON}  # the Monday of the fetch's week
    assert {r["fetched_at"] for r in t} == {NOW.replace(microsecond=0)}
    assert {"column": "Name", "text": "Gakenke - Kayanza - Washed (Ecotact)"} in t[1]["cells"]

    # the transform reads raw and derives each lot now; nothing derived is stored
    offers = importers.offers(lake)
    assert len(offers) == 8 and offers[0]["importer"] == "cafe_imports" and offers[0]["lot_id"] == "25973"
    assert offers[0]["process_type"] == "washed"

    # later the same week: nothing is fetched, nothing rewritten
    calls.clear()
    with _client(_pages(), calls) as client:
        again = importers.snapshot(lake, client, now=lambda: datetime(2026, 11, 15, tzinfo=UTC))
    assert [r["status"] for r in again] == ["skipped", "skipped"] and calls == []
    with pytest.raises(FileExistsError):  # immutable, as every lake file
        lake.write("importer_raw", importers.path("cafe_imports", MON), t)


def test_a_better_rule_rederives_every_stored_week(tmp_path, monkeypatch):
    lake = _local(tmp_path)
    with _client(_pages(), []) as client:
        importers.snapshot(lake, client, now=lambda: NOW)
        importers.snapshot(lake, client, now=lambda: NEXT)
    monkeypatch.setattr(rules, "RULES_VERSION", "99")
    monkeypatch.setattr(rules, "process", lambda stated, name: "washed")
    offers = importers.offers(lake)
    assert len(offers) == 16 and {o["week"] for o in offers} == {MON, date(2026, 11, 16)}
    assert {(o["rules_version"], o["process_type"]) for o in offers} == {("99", "washed")}


def test_a_thinned_page_fails_unless_the_drop_is_accepted(tmp_path):
    lake = _local(tmp_path)
    with _client(_pages(), []) as client:
        importers.snapshot(lake, client, now=lambda: NOW)
    thin = _pages(ia=_rows(IA_HTML, 1))  # 1 lot where last week had 4
    with _client(thin, []) as client:
        res = importers.snapshot(lake, client, now=lambda: NEXT)
    assert [r["status"] for r in res] == ["stored", "failed"]
    assert "under 50% of the 4" in res[1]["error"] and "--accept-drop" in res[1]["error"]
    assert not (lake.root / importers.path("interamerican", date(2026, 11, 16))).exists()
    with _client(thin, []) as client:
        res = importers.snapshot(lake, client, ["interamerican"], now=lambda: NEXT, accept_drop=True)
    assert res[0]["status"] == "stored" and res[0]["rows"] == 1 and res[0]["last_week_rows"] == 4
    # a smaller drop passes on its own
    assert (
        len(importers.parse("interamerican", _rows(IA_HTML, 2)).lots) >= importers.MIN_SHARE_OF_LAST_WEEK * 4
    )


def test_a_changed_page_fails_that_importer_and_writes_nothing_for_it(tmp_path):
    lake = _local(tmp_path)
    with _client(_pages(ci="<html><body>maintenance</body></html>"), []) as client:
        res = importers.snapshot(lake, client, now=lambda: NOW)
    assert [r["status"] for r in res] == ["failed", "stored"]
    assert "LayoutChanged" in res[0]["error"]
    assert not (lake.root / importers.path("cafe_imports", MON)).exists()


def test_a_failed_fetch_fails_loudly(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(403, text="no")

    with PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(handler)) as client:
        res = importers.snapshot(_local(tmp_path), client, now=lambda: NOW)
    assert [r["status"] for r in res] == ["failed", "failed"] and all("HTTP 403" in r["error"] for r in res)


def test_robots_disallow_is_respected(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    with PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(handler)) as client:
        res = importers.snapshot(_local(tmp_path), client, now=lambda: NOW)
    assert all(r["status"] == "failed" for r in res)
    assert set(calls) == {"/robots.txt"}  # never the page itself


# --- the private dataset --------------------------------------------------------------------------------------------


class FakeApi:
    def __init__(self, private: bool = True, existing: tuple[str, ...] = (), exists: bool = False):
        self.private, self.existing, self.calls, self.exists = (
            private,
            set(existing),
            [],
            exists or bool(existing),
        )

    def repo_exists(self, repo_id, repo_type):
        return self.exists

    def create_repo(self, repo_id, repo_type, private, exist_ok):
        self.calls.append(("create", repo_id, private, exist_ok))
        self.exists = True

    def dataset_info(self, repo_id):
        return types.SimpleNamespace(private=self.private)

    def list_repo_files(self, repo_id, repo_type):
        return sorted(self.existing)

    def get_paths_info(self, repo_id, paths, repo_type):
        return [types.SimpleNamespace(path=p, lfs=None, blob_id=None) for p in paths if p in self.existing]

    def upload_file(self, path_or_fileobj, path_in_repo, repo_id, repo_type, commit_message):
        self.calls.append(("card", path_in_repo))
        self.existing.add(path_in_repo)

    def hf_hub_download(self, repo_id, filename, repo_type, local_dir):
        return str(self.store / filename)

    def create_commit(self, repo_id, repo_type, operations, commit_message):
        import shutil

        self.calls.append(("commit", sorted(op.path_in_repo for op in operations)))
        for op in operations:
            self.existing.add(op.path_in_repo)
            (self.store / op.path_in_repo).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(op.path_or_fileobj, self.store / op.path_in_repo)


def _remote(tmp_path, monkeypatch, api: FakeApi, repo: str = "someone/importers") -> Lake:
    import huggingface_hub

    api.store = tmp_path / "hub"
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda token=None: api)
    monkeypatch.setattr(importers, "HUB_RETRY_WAITS", ())
    for var in importers.OTHER_DATASETS:
        monkeypatch.delenv(var, raising=False)
    return Lake(f"hf://datasets/{repo}", staging=tmp_path / "staging", tables=IMPORTER_TABLES)


def test_remote_dataset_is_created_private_and_written_once_per_week(tmp_path, monkeypatch):
    api = FakeApi()
    lake = _remote(tmp_path, monkeypatch, api)
    with _client(_pages(), []) as client:
        res = importers.snapshot(lake, client, now=lambda: NOW)
    assert [r["status"] for r in res] == ["stored", "stored"]
    assert api.calls[:2] == [("create", "someone/importers", True, True), ("card", "README.md")]
    commits = [c[1] for c in api.calls if c[0] == "commit"]
    schema = "raw/week=0000-00-00/importer=schema.parquet"
    # one file per importer and week, each in its own commit
    assert commits == [
        sorted([schema, importers.path("cafe_imports", MON)]),
        [importers.path("interamerican", MON)],
    ]
    assert lake.staged_files() == [lake.root / schema]

    calls: list[str] = []
    with _client(_pages(), calls) as client:  # a fresh runner later that week: the dataset already has it
        again = importers.snapshot(lake, client, now=lambda: NOW)
    assert [r["status"] for r in again] == ["skipped", "skipped"] and calls == []
    assert sum(c == ("card", "README.md") for c in api.calls) == 1  # sent once; unchanged since

    # next week's thinned page is checked against the stored week read back from the dataset
    with _client(_pages(ia=_rows(IA_HTML, 1)), []) as client:
        res = importers.snapshot(lake, client, ["interamerican"], now=lambda: NEXT)
    assert res[0]["status"] == "failed" and "of the 4 in its last stored week" in res[0]["error"]


def test_refuses_a_dataset_that_is_not_private(tmp_path, monkeypatch):
    for private in (False, None):
        api = FakeApi(private=private)
        lake = _remote(tmp_path, monkeypatch, api)
        calls: list[str] = []
        with _client(_pages(), calls) as client, pytest.raises(RuntimeError, match="not private"):
            importers.snapshot(lake, client, now=lambda: NOW)
        assert calls == [] and not any(c[0] in ("commit", "card") for c in api.calls)


def test_refuses_a_dataset_holding_anything_else(tmp_path, monkeypatch):
    api = FakeApi(existing=("README.md", "photos/p1.parquet"))
    lake = _remote(tmp_path, monkeypatch, api)
    with pytest.raises(RuntimeError, match="holds files this job didn't write \\(photos\\)"):
        importers.ensure_private(lake)
    assert api.calls == []  # no card replaced, nothing created


@pytest.mark.parametrize("var", list(importers.OTHER_DATASETS))
def test_refuses_the_lake_or_any_other_dataset(tmp_path, monkeypatch, var):
    # by its variable, compared case-insensitively
    api = FakeApi()
    lake = _remote(tmp_path, monkeypatch, api, repo="Someone/Importers")
    monkeypatch.setenv(var, "hf://datasets/someone/importers@main")
    with pytest.raises(RuntimeError, match=f"same dataset as {var}"):
        importers.ensure_private(lake)
    # and by its default name, where CI doesn't set the variable
    lake = _remote(
        tmp_path, monkeypatch, api, repo=importers.OTHER_DATASETS[var].removeprefix("hf://datasets/").upper()
    )
    with pytest.raises(RuntimeError, match=f"same dataset as {var}"):
        importers.ensure_private(lake)
    assert api.calls == []


def test_hub_calls_retry_only_transient_failures(monkeypatch):
    monkeypatch.setattr(importers, "HUB_RETRY_WAITS", (0.0, 0.0))
    monkeypatch.setattr(importers.time, "sleep", lambda s: None)

    def failing(code: int):
        calls = []

        def fn():
            calls.append(1)
            raise httpx.HTTPStatusError(
                "x", request=httpx.Request("GET", "https://hf.co"), response=httpx.Response(code)
            )

        return fn, calls

    for code, tries in ((403, 1), (404, 1), (429, 3), (503, 3)):
        fn, calls = failing(code)
        with pytest.raises(httpx.HTTPStatusError):
            importers._hub(fn)
        assert len(calls) == tries, code


# --- never public ---------------------------------------------------------------------------------------------------


def test_nothing_from_the_importers_can_reach_anything_public():
    """The importer table lives only in its own dataset: not a main-lake table (so dbt's lake roots and macro never
    name it), not referenced by any dbt model, contract, export, the site or the MCP server, and the release's public
    tables are the four public_* ones."""
    from coffeedata import public

    assert not set(IMPORTER_TABLES) & set(LAKE_TABLES)
    assert Lake.importers.__doc__ and "Never a dbt source" in Lake.importers.__doc__
    assert public.PUBLIC_TABLES == [
        "public_coffee",
        "public_roaster",
        "public_price_event",
        "public_market_weekly",
    ]
    root = Path(__file__).parent.parent
    folders = {GLOBS[t].split("/")[0] for t in IMPORTER_TABLES}
    macro = (root / "dbt/macros/lake.sql").read_text()
    assert not any(f"'{f}/" in macro for f in folders)
    sources = [
        *(root / "dbt").rglob("*.sql"),
        *(root / "dbt").rglob("*.yml"),
        *(root / "contracts").rglob("*.*"),
        *(root / "marketplace").rglob("*.*"),
        *(root / "src/coffeedata/templates").rglob("*.*"),
        *(
            root / "src/coffeedata" / f
            for f in ("public.py", "site.py", "snowflake_load.py", "mcp_server.py")
        ),
    ]
    for f in sources:
        if f.is_file() and "__pycache__" not in f.parts:
            text = f.read_text(errors="ignore").lower()
            assert "importer" not in text and "coffeedata_importers" not in text, f
    cli = (root / "src/coffeedata/cli.py").read_text()
    assert cli.count("Lake.importers()") == 1  # importer-snapshot, nothing else


# --- the command and the workflow -----------------------------------------------------------------------------------


def test_cli_exits_1_when_an_importer_failed(tmp_path, monkeypatch, capsys):
    from coffeedata import cli

    monkeypatch.setenv("COFFEEDATA_IMPORTERS", str(tmp_path / "importers"))
    monkeypatch.setattr(
        importers,
        "snapshot",
        lambda *a, **k: [{"importer": "cafe_imports", "status": "failed", "error": "x"}],
    )
    with pytest.raises(SystemExit) as e:
        cli.main(["importer-snapshot"])
    assert e.value.code == 1 and '"failed"' in capsys.readouterr().out


def test_cli_refuses_a_local_lake_in_ci_and_an_unknown_importer(tmp_path, monkeypatch):
    from coffeedata import cli

    monkeypatch.setenv("COFFEEDATA_IMPORTERS", str(tmp_path / "importers"))
    with pytest.raises(SystemExit, match="must be an hf://datasets/ URL"):
        cli.main(["importer-snapshot", "--require-hub"])
    with pytest.raises(SystemExit, match="no importer royal"):
        cli.main(["importer-snapshot", "--importer", "royal"])
    # accepting a drop covers one importer, named: never every importer in the run
    for argv in (
        ["--accept-drop"],
        ["--accept-drop", "--importer", "cafe_imports", "--importer", "interamerican"],
    ):
        with pytest.raises(SystemExit, match="exactly one --importer"):
            cli.main(["importer-snapshot", *argv])


def test_workflow_runs_on_main_only_and_never_on_pull_requests():
    """Its job holds a Hub write token: like eval-floors, a schedule and dispatch only, and the job runs on main."""
    wf = yaml.safe_load((Path(__file__).parent.parent / ".github/workflows/importers.yml").read_text())
    assert set(wf[True]) == {"schedule", "workflow_dispatch"}  # YAML 1.1 reads `on:` as True
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"importer", "accept_drop"}
    assert inputs["importer"]["options"] == ["all", *importers.IMPORTERS]
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        assert job.get("if") == "github.ref == 'refs/heads/main'", name
        assert job["steps"][0]["with"]["persist-credentials"] is False
        # the token goes to the one step that needs it, never to the whole job
        assert "HF_TOKEN" not in str(job.get("env", {}))
        holders = [s for s in job["steps"] if "HF_TOKEN" in str(s.get("env", {}))]
        assert len(holders) == 1 and "importer-snapshot --require-hub" in holders[0]["run"]
