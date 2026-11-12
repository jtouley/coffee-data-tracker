"""Royal Coffee, the third importer (coffeedata.importers, importer_rules): its Shopify listing read page by page,
each product kept word for word without prices or images, raw only and immutable per week like the other importers,
its values derived by the same private dbt project, and no network call to the store from any test or CI job.

Every product here is invented (tests/fixtures/importers/royal_synthetic_products.json) and every host is made up:
no AI agent may access Royal's site, so nothing in this file was built from it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
import yaml

from coffeedata import importer_rules as rules
from coffeedata import importers
from coffeedata.http import USER_AGENT, FetchError, PoliteClient
from coffeedata.lake import IMPORTER_TABLES, Lake

from .lakehelp import importer_offers, needs_dbt

ROOT = Path(__file__).parent.parent
FIXTURE = json.loads((Path(__file__).parent / "fixtures/importers/royal_synthetic_products.json").read_text())
PRODUCTS = FIXTURE["products"]
FAKE = "https://shop.example"  # a made-up store
LISTING = f"{FAKE}/products.json"
MON = date(2026, 11, 9)
NOW = datetime(2026, 11, 11, 17, 37, 5, tzinfo=UTC)  # a Wednesday in MON's week
# Royal's address as it could be written in any file: its host, with or without www.
ROYAL_HOST = re.compile(r"(?<![\w-])(?:www\.)?royalcoffee\.com(?![\w-])", re.I)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, no_network):
    """No connection off this machine, no Royal address or drop override in the environment, and no wait between
    listing pages (test_pages_are_spaced checks the wait), for every test here."""
    for var in (
        importers.ROYAL_URL_VAR,
        importers.ROYAL_ACCEPT_DROP_VAR,
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_EVENT_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(importers, "ROYAL_PAGE_INTERVAL", 0.0)


def _store(pages: list[list[dict]], calls: list[httpx.Request], robots: str = "User-agent: *\nDisallow:\n"):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        assert request.url.host == "shop.example" and request.url.path == "/products.json"
        n = int(request.url.params["page"])
        body = {"products": pages[n - 1] if n <= len(pages) else []}
        return httpx.Response(200, json=body)

    return PoliteClient(min_interval=0, retries=1, transport=httpx.MockTransport(handler))


def _pages_of(n: int) -> list[list[dict]]:
    return [PRODUCTS[i : i + n] for i in range(0, len(PRODUCTS), n)]


def test_the_fixture_is_labelled_synthetic_and_invented():
    assert FIXTURE["_synthetic"].startswith("SYNTHETIC FIXTURE")
    assert {p["vendor"] for p in PRODUCTS} == {"Synthetic Importer"}
    text = json.dumps(FIXTURE)
    assert not ROYAL_HOST.search(text)
    hosts = set(re.findall(r"https?://([^/\"]+)", text))
    assert hosts == {"cdn.example.invalid"}


def _pages_asked(calls: list[httpx.Request]) -> list[str | None]:
    return [c.url.params.get("page") for c in calls if c.url.path != "/robots.txt"]


def test_listing_is_read_with_the_fewest_pages_politely_and_never_logged_in(monkeypatch):
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 4)
    calls: list[httpx.Request] = []
    # last week had these 6: the short second page ends the listing, so the empty third is never asked for
    with _store(_pages_of(4), calls) as client:
        parsed = importers.read_royal(client, LISTING, last=6)
    assert len(parsed.lots) == 6 and parsed.unreadable_rows == 0
    assert [(c.url.path, dict(c.url.params)) for c in calls] == [
        ("/robots.txt", {}),
        ("/products.json", {"limit": "4", "page": "1"}),
        ("/products.json", {"limit": "4", "page": "2"}),
    ]
    for c in calls:
        assert c.headers["user-agent"] == USER_AGENT
        assert "authorization" not in c.headers and "cookie" not in c.headers


def test_an_early_short_page_doesnt_end_the_listing(monkeypatch):
    """Shopify can return a short page with more behind it: before last week's count is reached (or with no
    history), only an empty page ends the listing."""
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 4)
    calls: list[httpx.Request] = []
    with _store([PRODUCTS[:2], PRODUCTS[2:6]], calls) as client:  # page 1 short, page 2 full
        assert len(importers.read_royal(client, LISTING, last=6).lots) == 6
    assert _pages_asked(calls) == ["1", "2", "3"]
    calls.clear()  # no history: a short page is followed by one more request
    with _store(_pages_of(4), calls) as client:
        assert len(importers.read_royal(client, LISTING).lots) == 6
    assert _pages_asked(calls) == ["1", "2", "3"]


def test_a_full_last_page_costs_one_empty_page(monkeypatch):
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 3)
    calls: list[httpx.Request] = []
    with _store(_pages_of(3), calls) as client:
        assert len(importers.read_royal(client, LISTING, last=6).lots) == 6
    assert _pages_asked(calls) == ["1", "2", "3"]


@pytest.mark.parametrize(
    "pages",
    [
        [PRODUCTS[:3]] * 50,  # a store that ignores `page`, answering the first page every time
        [PRODUCTS[:3], PRODUCTS[2:5]],  # pages that overlap
        [PRODUCTS[:2], PRODUCTS[:2]],  # a short page, then the same one again
    ],
)
def test_a_store_that_ignores_page_fails_instead_of_storing_a_truncated_week(monkeypatch, pages):
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 3)
    calls: list[httpx.Request] = []
    with _store(pages, calls) as client, pytest.raises(importers.LayoutChanged, match="ignores `page`"):
        importers.read_royal(client, LISTING)
    assert _pages_asked(calls) == ["1", "2"]


def test_a_listing_that_never_ends_fails_instead_of_paging_forever(monkeypatch):
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 1)
    monkeypatch.setattr(importers, "ROYAL_MAX_PAGES", 3)
    calls: list[httpx.Request] = []
    with (
        _store([[p] for p in PRODUCTS], calls) as client,
        pytest.raises(importers.LayoutChanged, match="pages"),
    ):
        importers.read_royal(client, LISTING)
    assert len([c for c in calls if c.url.path != "/robots.txt"]) == 3


@pytest.mark.parametrize(
    "robots", ["User-agent: *\nDisallow: /\n", "User-agent: *\nDisallow: /products.json\n"]
)
def test_robots_disallow_is_honored(robots):
    calls: list[httpx.Request] = []
    with _store(_pages_of(250), calls, robots=robots) as client, pytest.raises(FetchError):
        importers.read_royal(client, LISTING)
    assert [c.url.path for c in calls] == ["/robots.txt"]


def test_a_full_page_with_nothing_keyable_fails_at_once(monkeypatch):
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 3)
    calls: list[httpx.Request] = []
    no_ids = [{k: v for k, v in p.items() if k != "id"} for p in PRODUCTS[:3]]
    with _store([no_ids] * 30, calls) as client, pytest.raises(importers.LayoutChanged, match="no product"):
        importers.read_royal(client, LISTING)
    assert _pages_asked(calls) == ["1"]


def test_pages_are_spaced(monkeypatch):
    """ROYAL_PAGE_INTERVAL before each page after the first, on top of the client's own per-host spacing."""
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 2)
    monkeypatch.setattr(importers, "ROYAL_PAGE_INTERVAL", 15.0)
    waits: list[float] = []
    with _store(_pages_of(2), []) as client:
        importers.read_royal(client, LISTING, last=6, sleep=waits.append)
    assert waits == [15.0, 15.0, 15.0]  # before pages 2, 3 and the empty 4th


def test_the_weekly_command_builds_the_one_client_allowed_a_schedule_only_host(monkeypatch):
    from coffeedata import cli

    seen = {}

    def fake(lake, client, *a, **k):
        seen["ok"] = client.schedule_only_ok
        seen["deadline"] = client.deadline is not None
        return []

    monkeypatch.setattr(importers, "snapshot", fake)
    try:
        cli.main(["importer-snapshot"])
    except SystemExit as e:
        assert e.code in (0, None)
    assert seen == {"ok": True, "deadline": True}
    assert PoliteClient().schedule_only_ok is False  # every other client refuses it


def test_each_product_is_kept_word_for_word_without_prices_or_images():
    lots = importers.parse_royal(PRODUCTS, LISTING).lots
    assert [lot.row for lot in lots] == list(range(6))
    first = lots[0].cells
    assert list(first) == [*importers.ROYAL_PRODUCT_FIELDS, "body_text", "options", "variants"]
    p = PRODUCTS[0]
    assert first["title"] == p["title"] and first["handle"] == p["handle"] and first["id"] == str(p["id"])
    assert first["tags"].split("\n") == p["tags"]
    # body_html's words, every line of them, without markup or the image tag
    assert first["body_text"] == (
        "An invented example lot: smallholders deliver parchment to a shared dryer, and the cup is bright and sweet.\n"
        "Altitude: 1,600–1,900 masl\nHarvest: October – January"
    )
    assert json.loads(first["options"]) == [{"name": "Size", "values": ["1 bag (70 kg)", "Sample (200 g)"]}]
    variants = json.loads(first["variants"])
    assert [set(v) for v in variants] == [set(importers.ROYAL_VARIANT_FIELDS)] * 2
    assert variants[0]["sku"] == "SYN-70412-B" and variants[1]["available"] is False
    assert lots[0].url == f"{FAKE}/products/{p['handle']}"
    for lot in lots:
        stored = json.dumps(lot.cells)
        for price in (
            "412.00",
            "450.00",
            "388.50",
            "45.00",
            "price",
            "compare_at",
            "cdn.example",
            "<img",
            "src",
        ):
            assert price not in stored, price


def test_a_changed_listing_fails_loudly():
    with pytest.raises(importers.LayoutChanged, match="zero readable rows"):
        importers.parse_royal([], LISTING)
    unreadable = [{k: v for k, v in p.items() if k != "handle"} for p in PRODUCTS[:2]] + PRODUCTS[2:]
    with pytest.raises(importers.LayoutChanged, match="unreadable"):
        importers.parse_royal(unreadable, LISTING)

    def not_a_listing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(200, json={"items": []})

    transport = httpx.MockTransport(not_a_listing)
    client = PoliteClient(min_interval=0, retries=1, transport=transport)
    with client, pytest.raises(importers.LayoutChanged, match="not a /products.json"):
        importers.read_royal(client, LISTING)


def _many(n: int) -> list[dict]:
    """n invented lots, each a copy of the fixture's second product with its own id, handle and lot number."""
    base = PRODUCTS[1]
    return [
        {
            **base,
            "id": 9500000000000 + i,
            "handle": f"{base['handle']}-{i}",
            "title": f"Invented Lot {80000 + i}",
        }
        for i in range(n)
    ]


@pytest.mark.parametrize(
    "where, value",
    [
        # #140's wider backstop: a currency amount, an amount per weight, a labelled price
        ("body_html", "<p>Producers were paid a $0.30/lb premium.</p>"),
        ("body_html", "<p>Offered at 4.25 per kg</p>"),
        ("body_html", "<p>Price: 4.25</p>"),
        ("tags", ["Process: Washed", "FOB 3.90 USD"]),
    ],
)
def test_a_field_mentioning_a_price_is_left_out_of_that_product_only(where, value):
    """One product's prose about a premium never fails the week: that field is left out of that product (never
    stored), and counted. Above MAX_BAD_SHARE of products, the run fails, as too many unreadable rows do."""
    field = "body_text" if where == "body_html" else where
    products = _many(20)
    products[0] = {**products[0], where: value}
    parsed = importers.parse_royal(products, LISTING)
    assert len(parsed.lots) == 20 and parsed.dropped_prices == 1
    assert parsed.lots[0].cells[field] is None
    assert all(lot.cells[field] for lot in parsed.lots[1:])
    for k in range(3):  # 3 of 20 (15%)
        products[k] = {**products[k], where: value}
    with pytest.raises(importers.LayoutChanged, match="3 of 20 products mention a price"):
        importers.parse_royal(products, LISTING)


def test_products_without_an_id_are_each_counted(monkeypatch):
    """Never merged under one key before the MAX_BAD_SHARE check: 4 of 34 is over 10%, 3 of 33 isn't."""
    for missing, ok in ((4, False), (3, True)):
        products = _many(30) + [{k: v for k, v in p.items() if k != "id"} for p in _many(missing)]
        with _store([products], []) as client:
            if ok:
                assert importers.read_royal(client, LISTING).unreadable_rows == missing
            else:
                with pytest.raises(
                    importers.LayoutChanged, match=f"{missing} of {30 + missing} rows unreadable"
                ):
                    importers.read_royal(client, LISTING)


def _with_fake_store(monkeypatch):
    royal = dataclasses.replace(importers.IMPORTERS[importers.ROYAL], locate=lambda: LISTING)
    monkeypatch.setitem(importers.IMPORTERS, importers.ROYAL, royal)


def test_raw_only_one_immutable_file_a_week(tmp_path, monkeypatch):
    _with_fake_store(monkeypatch)
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    calls: list[httpx.Request] = []
    with _store(_pages_of(250), calls) as client:
        res = importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
    assert [(r["importer"], r["week"], r["status"], r["rows"]) for r in res] == [
        ("royal_coffee", "2026-11-09", "stored", 6)
    ]
    assert sorted(f.relative_to(lake.root).as_posix() for f in lake.root.rglob("*.parquet")) == [
        "raw/week=0000-00-00/importer=schema.parquet",
        importers.path(importers.ROYAL, MON),
    ]
    raw = pq.read_table(lake.root / importers.path(importers.ROYAL, MON)).to_pylist()
    assert {(r["importer"], r["source_url"], r["week"]) for r in raw} == {("royal_coffee", LISTING, MON)}
    assert [r["url"] for r in raw] == [f"{FAKE}/products/{p['handle']}" for p in PRODUCTS]
    # nothing derived is stored: the private dbt project derives it (test_rules_on_the_synthetic_products)

    calls.clear()  # later that week: not fetched again, not rewritten
    with _store(_pages_of(250), calls) as client:
        again = importers.snapshot(
            lake, client, [importers.ROYAL], now=lambda: datetime(2026, 11, 14, tzinfo=UTC)
        )
    assert again[0]["status"] == "skipped" and calls == []
    with pytest.raises(FileExistsError):
        lake.write("importer_raw", importers.path(importers.ROYAL, MON), raw)


def test_a_thinned_listing_fails_like_the_other_importers(tmp_path, monkeypatch):
    _with_fake_store(monkeypatch)
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with _store(_pages_of(250), []) as client:
        importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
    with _store([PRODUCTS[:2]], []) as client:
        res = importers.snapshot(
            lake, client, [importers.ROYAL], now=lambda: datetime(2026, 11, 18, tzinfo=UTC)
        )
    assert res[0]["status"] == "failed" and "thinned" in res[0]["error"]
    # the way to accept it: the owner's week-scoped variable, never --accept-drop (a dispatch never fetches Royal)
    assert "ROYAL_ACCEPT_DROP_WEEK=2026-11-16" in res[0]["error"] and "--accept-drop" not in res[0]["error"]


def test_the_value_a_failed_week_names_is_accepted_by_the_next_weekly_run(tmp_path, monkeypatch):
    """The workflow runs once a week: week N fails and names N's Monday; the owner sets exactly that; week N+1's
    scheduled first attempt stores the thinned listing. A week later the value has expired."""
    _with_fake_store(monkeypatch)
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with _store(_pages_of(250), []) as client:  # week of 2026-11-09: 6 lots
        importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
    for k, v in SCHEDULED.items():
        monkeypatch.setenv(k, v)
    week_n = datetime(2026, 11, 18, 17, 37, tzinfo=UTC)  # week of 2026-11-16: thinned, fails
    with _store([PRODUCTS[:2]], []) as client:
        failed = importers.snapshot(lake, client, [importers.ROYAL], now=lambda: week_n)
    assert failed[0]["status"] == "failed"
    m = re.search(r"ROYAL_ACCEPT_DROP_WEEK=(\S+?);", failed[0]["error"])
    assert m and m.group(1) == "2026-11-16"
    monkeypatch.setenv("ROYAL_ACCEPT_DROP_WEEK", m.group(1))
    with _store([PRODUCTS[:2]], []) as client:  # week N+1, the next scheduled run
        res = importers.snapshot(lake, client, [importers.ROYAL], now=lambda: week_n + timedelta(days=7))
    assert res[0]["status"] == "stored" and res[0]["accepted_drop"] is True
    assert not importers.royal_accepts_drop(date(2026, 11, 30))[0]  # week N+2: expired


SCHEDULED = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_WORKFLOW": "importers",
    "GITHUB_EVENT_NAME": "schedule",
    "GITHUB_RUN_ATTEMPT": "1",
}


@pytest.mark.parametrize(
    "env, accepted, note",
    [
        ({**SCHEDULED, "ROYAL_ACCEPT_DROP_WEEK": "2026-11-16"}, True, None),
        (
            {**SCHEDULED, "ROYAL_ACCEPT_DROP_WEEK": "2026-11-09"},
            True,
            None,
        ),  # last week's: the week that failed
        ({**SCHEDULED, "ROYAL_ACCEPT_DROP_WEEK": "2026-11-02"}, False, "isn't this week's"),  # expired
        ({**SCHEDULED, "ROYAL_ACCEPT_DROP_WEEK": "2026-11-23"}, False, "isn't this week's"),  # a future week
        ({**SCHEDULED, "ROYAL_ACCEPT_DROP_WEEK": "yes"}, False, "isn't this week's"),
        (
            {**SCHEDULED, "GITHUB_RUN_ATTEMPT": "2", "ROYAL_ACCEPT_DROP_WEEK": "2026-11-16"},
            False,
            "first attempt",
        ),
        (
            {**SCHEDULED, "GITHUB_EVENT_NAME": "workflow_dispatch", "ROYAL_ACCEPT_DROP_WEEK": "2026-11-16"},
            False,
            "first attempt",
        ),
        (SCHEDULED, False, None),
    ],
)
def test_a_real_drop_at_royal_is_accepted_for_one_week_by_the_owners_variable(
    env, accepted, note, tmp_path, monkeypatch
):
    assert importers.royal_accepts_drop(date(2026, 11, 16), env)[0] is accepted
    _with_fake_store(monkeypatch)
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with _store(_pages_of(250), []) as client:
        importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with _store([PRODUCTS[:2]], []) as client:
        res = importers.snapshot(
            lake, client, [importers.ROYAL], now=lambda: datetime(2026, 11, 18, tzinfo=UTC)
        )
    assert res[0]["status"] == ("stored" if accepted else "failed")
    assert res[0].get("accepted_drop") is (True if accepted else None)
    if note:
        assert note in importers.royal_accepts_drop(date(2026, 11, 16), env)[1]


# --- private: the same private dataset checks as the other importers ---------------------------------------------


def test_royal_goes_only_to_the_private_importers_dataset(tmp_path, monkeypatch):
    """#140's private-dataset checks cover Royal: its one raw file a week is committed to the private importers
    dataset, after the privacy check, and never to a dataset that isn't private."""
    from tests.test_importers import FakeApi, _remote

    _with_fake_store(monkeypatch)
    api = FakeApi()
    lake = _remote(tmp_path, monkeypatch, api)
    with _store(_pages_of(250), []) as client:
        res = importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
    assert res[0]["status"] == "stored"
    assert api.calls[:2] == [("create", "someone/importers", True, True), ("card", "README.md")]
    assert [c[1] for c in api.calls if c[0] == "commit"] == [
        sorted(["raw/week=0000-00-00/importer=schema.parquet", importers.path(importers.ROYAL, MON)])
    ]

    for private in (False, None):  # a dataset that isn't private: refused before Royal is asked for anything
        api = FakeApi(private=private)
        lake = _remote(tmp_path / str(private), monkeypatch, api)
        calls: list[httpx.Request] = []
        with _store(_pages_of(250), calls) as client, pytest.raises(RuntimeError, match="not private"):
            importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
        assert calls == [] and not any(c[0] in ("commit", "card") for c in api.calls)


# --- no test and no CI job ever reaches Royal -----------------------------------------------------------------------


def test_without_its_address_royal_is_skipped_and_nothing_is_fetched(tmp_path):
    calls: list[httpx.Request] = []
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with _store([], calls) as client:
        res = importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
    assert res[0]["status"] == "skipped" and calls == []
    assert res[0]["reason"] == "fetched only on a scheduled run's first attempt"
    assert importers.IMPORTERS[importers.ROYAL].url is None


@pytest.mark.parametrize(
    "context, status",
    [
        # a re-run keeps the `schedule` event: the workflow gives it no address, so Royal is skipped, nothing asked
        ({**SCHEDULED, "GITHUB_RUN_ATTEMPT": "2"}, "skipped"),
        ({**SCHEDULED, "GITHUB_RUN_ATTEMPT": "3"}, "skipped"),
        ({**SCHEDULED, "GITHUB_EVENT_NAME": "workflow_dispatch"}, "skipped"),
        # the scheduled first attempt without the address is a workflow edit gone wrong: fail, don't pass as a skip
        (SCHEDULED, "failed"),
    ],
)
def test_without_its_address_a_rerun_skips_royal_and_a_scheduled_first_attempt_fails(
    context, status, tmp_path, monkeypatch
):
    for k, v in context.items():
        monkeypatch.setenv(k, v)
    calls: list[httpx.Request] = []
    with _store(_pages_of(250), calls) as client:
        res = importers.snapshot(
            Lake(str(tmp_path / "i"), tables=IMPORTER_TABLES), client, [importers.ROYAL], now=lambda: NOW
        )
    assert res[0]["status"] == status and calls == []
    if status == "failed":
        assert "empty on the scheduled run's first attempt" in res[0]["error"]


@pytest.mark.parametrize(
    "context",
    [
        {},  # a laptop or an agent's sandbox
        {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "ci", "GITHUB_EVENT_NAME": "pull_request"},
        {**SCHEDULED, "GITHUB_EVENT_NAME": "workflow_dispatch"},
        {**SCHEDULED, "GITHUB_RUN_ATTEMPT": "2"},  # a re-run of a scheduled run keeps its event
        {**{k: v for k, v in SCHEDULED.items() if k != "GITHUB_RUN_ATTEMPT"}},  # no attempt number
        SCHEDULED,  # but under pytest
    ],
)
def test_its_address_is_refused_anywhere_but_the_scheduled_workflow(context, tmp_path, monkeypatch):
    env = {importers.ROYAL_URL_VAR: FAKE, **context}
    with pytest.raises(importers.NotAllowedHere):
        importers.royal_listing_url(env)
    # through the weekly job too: Royal fails loudly, and nothing is requested
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    calls: list[httpx.Request] = []
    with _store(_pages_of(250), calls) as client:
        res = importers.snapshot(
            Lake(str(tmp_path / "i"), tables=IMPORTER_TABLES),
            client,
            [importers.ROYAL],
            now=lambda: NOW,
        )
    assert res[0]["status"] == "failed" and "NotAllowedHere" in res[0]["error"] and calls == []


def test_royals_address_is_only_in_the_scheduled_step_of_the_importers_workflow():
    """No test, fixture, script, config or other workflow names the store, and the one place that does hands it to
    the snapshot step only on a scheduled run. The roaster crawl doesn't list it either."""
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    naming = sorted(
        f for f in files if (ROOT / f).is_file() and ROYAL_HOST.search((ROOT / f).read_text(errors="ignore"))
    )
    assert naming == [".github/workflows/importers.yml"]

    wf = yaml.safe_load((ROOT / ".github/workflows/importers.yml").read_text())
    assert set(wf[True]) == {"schedule", "workflow_dispatch"}
    steps = [s for job in wf["jobs"].values() for s in job["steps"]]
    holders = [s for s in steps if ROYAL_HOST.search(json.dumps(s))]
    assert len(holders) == 1 and "importer-snapshot" in holders[0]["run"]
    # the address only on a scheduled run's first attempt, empty otherwise (this file never spells the address out)
    first = r"github\.event_name == 'schedule' && github\.run_attempt == '1'"
    given = holders[0]["env"][importers.ROYAL_URL_VAR]
    m = re.fullmatch(rf"\$\{{\{{ {first} && '(https://[^']+)' \|\| '' \}}\}}", given)
    assert m and ROYAL_HOST.fullmatch(m.group(1).removeprefix("https://"))
    assert [k for k, v in holders[0]["env"].items() if ROYAL_HOST.search(str(v))] == [importers.ROYAL_URL_VAR]
    # the owner's drop override, on the same scheduled first attempt only
    assert re.fullmatch(
        rf"\$\{{\{{ {first} && vars\.ROYAL_ACCEPT_DROP_WEEK \|\| '' \}}\}}",
        holders[0]["env"][importers.ROYAL_ACCEPT_DROP_VAR],
    )
    for wf_file in (ROOT / ".github/workflows").glob("*.y*ml"):
        if wf_file.name != "importers.yml":
            assert importers.ROYAL_URL_VAR not in wf_file.read_text(), wf_file
    assert wf["name"] == importers.ROYAL_WORKFLOW
    # What bounds a pass is the run's deadline (IMPORTERS_DEADLINE_S, from the client's start, all importers
    # together), not the job timeout. Leave 15 minutes of it for robots.txt, the other importers and the Hub calls.
    from coffeedata.http import MAX_CRAWL_DELAY

    room = importers.IMPORTERS_DEADLINE_S - 15 * 60
    # at Royal's own pace a full pass fits
    assert room >= importers.ROYAL_MAX_PAGES * importers.ROYAL_PAGE_INTERVAL
    # at the longest Crawl-delay the client honors it doesn't: about 90 pages fit, and a longer pass ends as a
    # recorded DeadlineReached with nothing written (test_a_run_past_its_deadline_is_a_recorded_failure). The
    # workflow's comment says so; if the deadline or the page cap changes, update it with this.
    fit = int(room // max(importers.ROYAL_PAGE_INTERVAL, MAX_CRAWL_DELAY))
    assert fit == 90 < importers.ROYAL_MAX_PAGES
    assert "about 90 pages fit" in (ROOT / ".github/workflows/importers.yml").read_text()
    # and the run itself stops a little before the job is killed, so a long pass is a recorded failure
    timeout_s = wf["jobs"]["snapshot"]["timeout-minutes"] * 60
    assert timeout_s - 20 * 60 <= importers.IMPORTERS_DEADLINE_S <= timeout_s - 10 * 60


def _workflow_host() -> str:
    """Royal's host, read from the one file that names it (so this file never spells it out)."""
    text = (ROOT / ".github/workflows/importers.yml").read_text()
    m = ROYAL_HOST.search(text)
    assert m
    return m.group(0)


def _equivalent_spellings(host: str) -> list[str]:
    """Spellings the HTTP library reads as `host` (it maps other dot characters to "." and ignores case, a trailing
    dot, userinfo and the port), and compatibility forms (full-width) a client could be handed."""
    dots = ("\u3002", "\uff0e", "\uff61")
    full = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in host)
    return [
        host.upper(),
        f"{host}.",
        *(host.replace(".", d) for d in dots),
        *(f"www{d}{host.replace('.', d)}" for d in dots),
        full,
        f"https://user@{host}:443/products.json",
        f"https://{host.upper()}./",
        f"https://{host.replace('.', dots[0])}/robots.txt",
    ]


def test_every_spelling_the_http_library_treats_as_the_store_is_refused(lake):
    """The refusal compares the host the HTTP library will actually connect to, so no equivalent spelling slips
    past it (a mock transport records any request; nothing leaves this machine either way)."""
    from datetime import UTC, datetime

    from coffeedata import http, registry

    host = _workflow_host()
    forms = _equivalent_spellings(host)
    for form in forms:
        assert http.schedule_only_host(form), ascii(form)
        url = form if "//" in form else f"https://{form}/products.json"
        with contextlib.suppress(
            httpx.InvalidURL
        ):  # the library reads it as the store, or rejects it outright
            assert httpx.URL(url).raw_host.decode().rstrip(".").removeprefix("www.") == host, ascii(form)
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, text=""))
    with PoliteClient(min_interval=0, retries=1, transport=transport) as client:
        for form in forms:
            url = form if "//" in form else f"https://{form}/products.json"
            with pytest.raises(http.ScheduleOnlyHost):
                client.get(url)
    assert calls == []
    # the roaster crawl's side: never a storefront, never in the registry, whatever the spelling
    for form in forms:
        assert not registry.is_storefront_host(registry.normalize_domain(form)), ascii(form)
    lake.write("registry", "registry/source=manual/x.parquet", [
        {"added_at": datetime.now(UTC), "source": "manual", "roaster_id": rid, "name": "Added"}
        for rid in [*(registry.normalize_domain(f) for f in forms), "realroaster.com"]
    ])  # fmt: skip
    ids = {s.roaster.roaster_id for s in registry.current(lake)}
    assert ids & {registry.normalize_domain(f) for f in forms} == set() and "realroaster.com" in ids
    # look-alikes that aren't the store stay allowed
    for other in (f"not{host}", f"{host}.evil.example", f"https://x.example/?ref={host}", "shop.example"):
        assert not http.schedule_only_host(other), other


def test_a_malformed_url_naming_the_store_is_refused_as_such(monkeypatch):
    """ScheduleOnlyHost (a Blocked, which callers handle), never a ValueError from parsing the URL again."""
    from coffeedata import http

    host = _workflow_host()
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    with PoliteClient(min_interval=0, retries=1, transport=transport) as client:
        for url in (f"https://[{host}/products.json", f"https://{host}]/", f"https://[x]({host})/"):
            with pytest.raises(http.ScheduleOnlyHost):
                client.get(url)
    assert calls == []


def test_a_run_past_its_deadline_is_a_recorded_failure(tmp_path, monkeypatch):
    """The weekly command's client stops at IMPORTERS_DEADLINE_S: a pass still running then fails as
    DeadlineReached, recorded per importer, nothing written, instead of the job being killed."""
    _with_fake_store(monkeypatch)
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    calls: list[httpx.Request] = []
    with _store(_pages_of(250), calls) as client:
        client.deadline = 0.0  # already passed
        res = importers.snapshot(lake, client, [importers.ROYAL], now=lambda: NOW)
    assert res[0]["status"] == "failed" and "DeadlineReached" in res[0]["error"] and calls == []
    assert not (lake.root / importers.path(importers.ROYAL, MON)).exists()


def test_no_other_client_can_reach_the_store_and_the_roaster_crawl_never_lists_it(lake, monkeypatch):
    """Royal is refused outright outside the importers job: every PoliteClient but the one cmd_importer_snapshot
    builds (robots.txt included), and the roaster registry, discovery and add-roaster, even when a lake addition
    names it."""
    from datetime import UTC, datetime

    from coffeedata import http, registry

    host = _workflow_host()
    for h in (host, f"www.{host}", f"https://{host}/products.json", f"shop.{host}"):
        assert http.schedule_only_host(h), h
    assert not http.schedule_only_host("shop.example") and not http.schedule_only_host(f"not{host}")
    # a value urlsplit can't read never raises: the store inside it is still refused, anything else isn't the store
    for bad in (f"[www.{host}](http", f"[{host}", f"{host}]", f"[x]({host})"):
        assert http.schedule_only_host(bad), bad

    # a client that isn't the importers job's refuses before any request, robots.txt included (a mock transport
    # records it; nothing leaves this machine either way)
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500))
    with PoliteClient(min_interval=0, retries=1, transport=transport) as client:
        with pytest.raises(http.ScheduleOnlyHost):
            client.get(f"https://{host}/products.json", limit=250, page=1)
        with pytest.raises(http.ScheduleOnlyHost):
            client.get(f"https://www.{host}/")
    assert calls == []

    # the importers job's client may: shown with a made-up host put on the list, never the store itself
    monkeypatch.setattr(
        http,
        "SCHEDULE_ONLY_HOSTS_SHA256",
        http.SCHEDULE_ONLY_HOSTS_SHA256 | {hashlib.sha256(b"shop.example").hexdigest()},
    )
    with _store(_pages_of(250), []) as client, pytest.raises(http.ScheduleOnlyHost):
        importers.read_royal(client, LISTING)
    with _store(_pages_of(250), []) as client:
        client.schedule_only_ok = True
        assert len(importers.read_royal(client, LISTING).lots) == 6

    # the roaster crawl: never a storefront, and dropped from the registry even when the lake adds it
    assert not registry.is_storefront_host(host) and not registry.is_storefront_host(f"www.{host}")
    lake.write("registry", "registry/source=manual/x.parquet", [
        {"added_at": datetime.now(UTC), "source": "manual", "roaster_id": rid, "name": "Added"}
        for rid in (host, f"shop.{host}", "realroaster.com")
    ])  # fmt: skip
    ids = {s.roaster.roaster_id for s in registry.current(lake)}
    assert "realroaster.com" in ids and not {host, f"shop.{host}"} & ids


BAD_WEBSITES = [
    "[www.roaster.example](http://roaster.example)",
    "[bad.example",
    "bad.example]",
    "http://[::1",
]


@pytest.mark.parametrize("bad", BAD_WEBSITES)
def test_a_malformed_host_never_breaks_discovery_the_registry_or_add_roaster(
    bad, lake, tmp_path, monkeypatch
):
    """schedule_only_host sits in shared code (registry, discovery, add-roaster): a malformed value is "not the
    store", never an exception."""
    from coffeedata import cli, http, registry

    assert http.schedule_only_host(bad) is False
    assert http.schedule_only_host(registry.normalize_domain(bad)) is False
    registry.is_storefront_host(registry.normalize_domain(bad))  # True or False, never raises
    lake.write("registry", "registry/source=manual/x.parquet", [
        {"added_at": datetime.now(UTC), "source": "manual", "roaster_id": rid, "name": "Added"}
        for rid in (registry.normalize_domain(bad), "realroaster.com")
    ])  # fmt: skip
    assert "realroaster.com" in {s.roaster.roaster_id for s in registry._current(lake, registry.SEED)}
    # add-roaster: refused as "not a roaster website", or accepted, but never a traceback
    form = tmp_path / "issue.md"
    form.write_text(f"### Roaster name\n\nAdded\n\n### Website\n\n{bad}\n\n### State\n\nOR\n")
    monkeypatch.setenv("COFFEEDATA_LAKE", str(tmp_path / "lake-add"))
    try:
        cli.main(["add-roaster", "--file", str(form)])
    except SystemExit as e:
        assert e.code in (0, None) or "not a roaster website" in str(e.code), e.code


# --- the rules: decide when the text says so, abstain otherwise --------------------------------------------------


# The synthetic products predate the real listing's shapes (their titles say "Lot 70412 - Harborview Warehouse" and
# their facts sit in "Label: value" lines, which Royal's real bodies don't have), so on them the rules mostly abstain:
# no five-digit lot ID part, so none reads as a lot (is_lot false) and none gets an origin, warehouse or status.
EXPECTED = [
    # (process as stated, process type, certifications, available)
    (None, "washed", None, True),
    ("Natural", "natural", ["rainforest_alliance"], True),
    (None, None, None, True),  # decaf
    (None, None, None, True),  # two processes
    (None, "anaerobic", None, False),  # two process tags as written; anaerobic wins
    (None, None, None, True),  # not a lot at all
]  # fmt: skip


@pytest.fixture(scope="module")
def derived(tmp_path_factory) -> dict[int, dict]:
    """The synthetic products, then every case of test_rules_abstain_when_unsure (row len(PRODUCTS) + its index), stored
    as one raw week of Royal lots and derived by the private dbt project (its unit tests passing first), by row."""
    from .smoke import require_dbt

    require_dbt()  # as needs_dbt does, before building
    tmp = tmp_path_factory.mktemp("royal-derived")
    lake = Lake(str(tmp / "importers"), tables=IMPORTER_TABLES)
    cells = [lot.cells for lot in importers.parse_royal(PRODUCTS, LISTING).lots] + [
        {"id": str(9_900_000_000_000 + k), **c} for k, (c, _, _) in enumerate(CASES)
    ]
    base = {
        "week": MON,
        "fetched_at": NOW,
        "importer": "royal_coffee",
        "source_url": LISTING,
        "parser_version": "t",
    }
    rows = [
        base | {"row": i, "url": None, "cells": [{"column": k, "text": v} for k, v in c.items()]}
        for i, c in enumerate(cells)
    ]
    lake.write("importer_raw", importers.path(importers.ROYAL, MON), rows)
    return {o["row"]: o for o in importer_offers(lake, tmp, unit_tests=True)}


@needs_dbt
@pytest.mark.parametrize("i", range(len(PRODUCTS)))
def test_rules_on_the_synthetic_products(i, derived):
    got = dict(derived[i])
    process, process_type, certs, available = EXPECTED[i]
    assert got.pop("problems") == []
    assert {k: got[k] for k in (
        "name", "is_lot", "lot_id", "warehouse", "status", "est_arrival", "bags", "origin", "origin_as_written", "region",
        "process", "process_type", "varieties", "altitude_min_m", "altitude_max_m", "harvest_months", "grower_type",
        "grower_name", "certifications", "not_certified", "is_available", "currently_offered", "lot_key",
        "rules_version",
    )} == {
        "name": PRODUCTS[i]["title"],
        "is_lot": False,
        "lot_id": None,
        "warehouse": None,
        "status": None,
        "est_arrival": None,
        "bags": None,  # Royal's listing never states a bag count
        "origin": None,
        "origin_as_written": None,
        "region": None,  # the body is prose: region, varieties, altitude and harvest are left for Claude
        "process": process,
        "process_type": process_type,
        "varieties": None,
        "altitude_min_m": None,
        "altitude_max_m": None,
        "harvest_months": None,
        "grower_type": None,
        "grower_name": None,
        "certifications": certs,
        "not_certified": None,  # the store never marks one absent: unknown, never "not certified"
        "is_available": available,
        "currently_offered": available,  # the only stored week is the latest
        "lot_key": f"royal_coffee:{PRODUCTS[i]['id']}",  # the Shopify id, never the listing position
        "rules_version": rules.ROYAL_RULES_VERSION,
    }  # fmt: skip


def _cells(
    title: str = "Example Lot",
    tags: list[str] = (),  # type: ignore[assignment]
    body: str = "",
    variants: str | None = None,
) -> dict[str, str | None]:
    return {
        "title": title,
        "tags": "\n".join(tags) or None,
        "body_text": body or None,
        "product_type": None,
        "variants": variants,
    }


# Titles in the real listing's shapes ("<ORIGIN ...> - [*contract*] - <lot ID> - [bags] - [date] - [SPOT] <WAREHOUSE>"),
# every value invented: no real title, lot ID, warehouse or producer.
CASES = [
    (_cells("COLOMBIA FT-FLO/USA ORGANIC HUILA EXAMPLE - *54321* - 12345 - GrainPro Bags - SPOT EXAMPLEWHSE"),
     "lot", "12345"),
    (_cells("Honduras Example Producer Lot 3 SHG - 23456-1 - SPOT EXAMPLEWHSE"), "lot", "23456-1"),  # not "Lot 3"
    (_cells("Kenya Example AA - 11111 - 22222 - SPOT EXAMPLEWHSE"), "lot", None),  # two IDs
    (_cells("Kenya Example AB - 2026 - SPOT EXAMPLEWHSE"), "lot", None),  # a year
    (_cells("Peru Example - 34567 - SPOT EXAMPLEWHSE C2"), "warehouse", "EXAMPLEWHSE C2"),
    (_cells("Peru Example - 34568 - May 2027 Shipment - EXAMPLEWHSE"), "status", None),  # not there yet
    (_cells("Peru Example - 34569 - SPOT EXAMPLEWHSE"), "status", "spot"),
    (_cells("Ethiopia Example Natural - 34570 - Sample"), "warehouse", None),  # not a warehouse code
    (_cells("Ethiopia Example Natural - 10574 - ORGANIC"), "warehouse", None),  # a word in capitals
    (_cells("Workshop: Invented Cupping Night About Kenya"), "origin", None),  # not a lot
    (_cells("Brazil Example - 34571 - ETA: Dec 12, 2026 - EXAMPLEWHSE"), "arrival", "Dec 12, 2026"),
    (_cells("Brazil Example - 10573 - Intend Ship: Dec 12, 2026 - EXAMPLEWHSE"), "arrival", None),
    (_cells("PAPUA NEW GUINEA EXAMPLE A - 45678 - SPOT EXAMPLEWHSE"), "origin", "Papua New Guinea"),
    (_cells("TIMOR LESTE EXAMPLE - 45679 - SPOT EXAMPLEWHSE"), "origin", "Timor-Leste"),
    (_cells("TIMOR ORGANIC EXAMPLE - 45680 - SPOT EXAMPLEWHSE"), "origin", "Timor-Leste"),
    (_cells("Decaf Komodo Example Water Process - 45681 - SPOT EXAMPLEWHSE"), "origin", "Indonesia"),
    (_cells("FLORES EXAMPLE - 45682 - SPOT EXAMPLEWHSE"), "origin", "Indonesia"),
    (_cells("Guatemala Example Las Flores Natural - 45683 - SPOT EXAMPLEWHSE"), "origin", "Guatemala"),
    (_cells("Colombia Peru Example Blend - 45684 - SPOT EXAMPLEWHSE"), "origin", None),
    (_cells("New Guinea Example - 45685 - SPOT EXAMPLEWHSE"), "origin", None),
    (_cells("Congo Example Kivu Washed - 45686 - SPOT EXAMPLEWHSE"), "origin", None),
    (_cells("PERU FT EXAMPLE - 56789 - SPOT EXAMPLEWHSE"), "certifications", ["fair_trade"]),
    (_cells("PERU FT-USA ORGANIC EXAMPLE - 56790 - SPOT EXAMPLEWHSE"), "certifications", ["organic", "fair_trade"]),
    (_cells("Mexico Example 30 ft Terraces - 56791 - SPOT EXAMPLEWHSE"), "certifications", None),  # feet
    (_cells("GUATEMALA FT ORGANIC BIRD FRIENDLY EXAMPLE - 56792"), "certifications", None),  # unmodeled program
    (_cells("Peru Non-Organic Example - 56793"), "certifications", None),
    (_cells(tags=["FTO"]), "certifications", None),  # Fair Trade Organic, or only Fair Trade?
    (_cells(tags=["Fair Trade", "Organic", "FTO"]), "certifications", ["organic", "fair_trade"]),
    (_cells("Ethiopia Example Natural - 67890", ["natural"], body="Invented: the farm also grows sugar cane."),
     "process", "natural"),  # the body's prose isn't read for decaf
    (_cells("Mexico Example Natural - 67891", ["decaf"]), "process", None),
    (_cells("Rwanda Example Washed - 67892", ["natural"]), "process", None),  # title and tag disagree
    (_cells("Sumatra Example - 67893", ["wet-hulled"]), "process", "honey"),
    (_cells("Colombia Example Anaerobic - 67894", ["natural"]), "process", "anaerobic"),  # anaerobic wins
    (_cells(variants='[{"available": false, "grams": 69041}]'), "available", False),
    (_cells(variants='[{"available": true, "grams": 69041}]'), "bag_kg", 69.0),
    (_cells("Peru Example - 67895 - 30.0 kg Bags", variants='[{"available": true, "grams": 30105}]'), "bag_kg", 30.0),
    (_cells(body="Region: Huila. Altitude: 1,800 masl"), "region", None),  # labels aren't read any more
]  # fmt: skip


@needs_dbt
@pytest.mark.parametrize("k", range(len(CASES)))
def test_rules_abstain_when_unsure(k, derived):
    _, field, expected = CASES[k]
    got = derived[len(PRODUCTS) + k]
    value = {
        "lot": got["lot_id"],
        "warehouse": got["warehouse"],
        "status": got["status"],
        "arrival": got["est_arrival"],
        "origin": got["origin"],
        "certifications": got["certifications"],
        "process": got["process_type"],
        "available": got["is_available"],
        "bag_kg": got["bag_kg"],
        "region": got["region"],
    }[field]
    assert value == expected
