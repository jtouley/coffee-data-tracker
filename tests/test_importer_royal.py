"""Royal Coffee, the third importer (coffeedata.importers, importer_rules): its Shopify listing read page by page,
each product kept word for word without prices or images, raw only and immutable per week like the other importers,
its values derived on read by the same transform, and no network call to the store from any test or CI job.

Every product here is invented (tests/fixtures/importers/royal_synthetic_products.json) and every host is made up:
no AI agent may access Royal's site, so nothing in this file was built from it.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
import yaml

from coffeedata import importer_rules as rules
from coffeedata import importers
from coffeedata.http import USER_AGENT, FetchError, PoliteClient
from coffeedata.lake import IMPORTER_TABLES, Lake

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
    """No connection off this machine, and no Royal address in the environment, for every test here."""
    monkeypatch.delenv(importers.ROYAL_URL_VAR, raising=False)


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


def test_listing_is_read_with_the_fewest_pages_politely_and_never_logged_in(monkeypatch):
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 4)
    calls: list[httpx.Request] = []
    with _store(_pages_of(4), calls) as client:
        parsed = importers.read_royal(client, LISTING)
    assert len(parsed.lots) == 6 and parsed.unreadable_rows == 0
    # robots.txt first, then two pages: the second is short, so the empty third is never asked for
    assert [(c.url.path, dict(c.url.params)) for c in calls] == [
        ("/robots.txt", {}),
        ("/products.json", {"limit": "4", "page": "1"}),
        ("/products.json", {"limit": "4", "page": "2"}),
    ]
    for c in calls:
        assert c.headers["user-agent"] == USER_AGENT
        assert "authorization" not in c.headers and "cookie" not in c.headers


def test_a_full_last_page_costs_one_empty_page_and_a_repeating_store_stops(monkeypatch):
    monkeypatch.setattr(importers, "ROYAL_PAGE_LIMIT", 3)
    calls: list[httpx.Request] = []
    with _store(_pages_of(3), calls) as client:
        assert len(importers.read_royal(client, LISTING).lots) == 6
    assert [c.url.params.get("page") for c in calls if c.url.path != "/robots.txt"] == ["1", "2", "3"]

    calls.clear()  # a store that ignores `page`, answering the first page every time
    with _store([PRODUCTS[:3]] * 50, calls) as client:
        assert len(importers.read_royal(client, LISTING).lots) == 3
    assert [c.url.params.get("page") for c in calls if c.url.path != "/robots.txt"] == ["1", "2"]


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


def test_robots_disallow_is_honored():
    calls: list[httpx.Request] = []
    with (
        _store(_pages_of(250), calls, robots="User-agent: *\nDisallow: /\n") as client,
        pytest.raises(FetchError),
    ):
        importers.read_royal(client, LISTING)
    assert [c.url.path for c in calls] == ["/robots.txt"]


def test_pages_are_spaced_by_the_cli_client():
    """The weekly command's client waits ROYAL_PAGE_INTERVAL between two requests to one host."""
    from coffeedata import cli

    seen = {}

    def fake(lake, client, *a, **k):
        seen["interval"] = client.min_interval
        return []

    orig = importers.snapshot
    importers.snapshot = fake
    try:
        cli.main(["importer-snapshot"])
    except SystemExit as e:
        assert e.code in (0, None)
    finally:
        importers.snapshot = orig
    assert seen["interval"] == importers.ROYAL_PAGE_INTERVAL >= 10


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
    # a price that got into a kept field (here, the body's prose) fails the run, as on the other importers
    # (#140's wider backstop: a currency amount, an amount per weight, a labelled price)
    for prose in ("<p>Now only $4.10/lb</p>", "<p>Offered at 4.25 per kg</p>", "<p>Price: 4.25</p>"):
        priced = [{**PRODUCTS[0], "body_html": prose}, *PRODUCTS[1:]]
        with pytest.raises(importers.LayoutChanged, match="a price in the 'body_text' field"):
            importers.parse_royal(priced, LISTING)
    tagged = [{**PRODUCTS[0], "tags": [*PRODUCTS[0]["tags"], "FOB 3.90 USD"]}, *PRODUCTS[1:]]
    with pytest.raises(importers.LayoutChanged, match="a price in the 'tags' field"):
        importers.parse_royal(tagged, LISTING)

    def not_a_listing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(200, json={"items": []})

    transport = httpx.MockTransport(not_a_listing)
    client = PoliteClient(min_interval=0, retries=1, transport=transport)
    with client, pytest.raises(importers.LayoutChanged, match="not a /products.json"):
        importers.read_royal(client, LISTING)


def _with_fake_store(monkeypatch):
    royal = dataclasses.replace(importers.IMPORTERS[importers.ROYAL], locate=lambda: LISTING)
    monkeypatch.setitem(importers.IMPORTERS, importers.ROYAL, royal)


def test_raw_only_one_immutable_file_a_week_and_values_derived_on_read(tmp_path, monkeypatch):
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
    # the same read-time transform as the other importers: derived from the stored cells, never stored
    derived = [o for o in importers.offers(lake) if o["importer"] == "royal_coffee"]
    assert [(o["lot_id"], o["warehouse"], o["process_type"]) for o in derived[:2]] == [
        ("70412", "Harborview", "washed"),
        ("70588", "Riverside", "natural"),
    ]
    assert derived[0]["name"] == PRODUCTS[0]["title"] and derived[0]["problems"] == []

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
    assert res[0]["status"] == "skipped" and res[0]["reason"] == "fetched on schedule only" and calls == []
    assert importers.IMPORTERS[importers.ROYAL].url is None


@pytest.mark.parametrize(
    "context",
    [
        {},  # a laptop or an agent's sandbox
        {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "ci", "GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_ACTIONS": "true", "GITHUB_WORKFLOW": "importers", "GITHUB_EVENT_NAME": "workflow_dispatch"},
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKFLOW": "importers",
            "GITHUB_EVENT_NAME": "schedule",
        },  # but pytest
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
    # the address only when the run is scheduled, empty otherwise (this file never spells the address out)
    given = holders[0]["env"][importers.ROYAL_URL_VAR]
    m = re.fullmatch(r"\$\{\{ github\.event_name == 'schedule' && '(https://[^']+)' \|\| '' \}\}", given)
    assert m and ROYAL_HOST.fullmatch(m.group(1).removeprefix("https://"))
    assert [k for k, v in holders[0]["env"].items() if ROYAL_HOST.search(str(v))] == [importers.ROYAL_URL_VAR]
    for wf_file in (ROOT / ".github/workflows").glob("*.y*ml"):
        if wf_file.name != "importers.yml":
            assert importers.ROYAL_URL_VAR not in wf_file.read_text(), wf_file
    assert wf["name"] == importers.ROYAL_WORKFLOW


# --- the rules: decide when the text says so, abstain otherwise --------------------------------------------------


EXPECTED = [
    # (lot, warehouse, origin, region, process, varieties, altitude, harvest, certifications)
    ("70412", "Harborview", "Colombia", "Huila", "washed", ["Castillo", "Caturra"], (1600, 1900),
     ["oct", "nov", "dec", "jan"], ["organic"]),
    ("70588", "Riverside", "Ethiopia", "Sidama", "natural", ["74110", "Heirloom"], (2000, 2250),
     ["nov", "dec", "jan"], ["rainforest_alliance"]),
    # decaf: no process; both certifications from the body's label
    ("70733", "Harborview", "Peru", "Cajamarca", None, None, (1750, 1750), None, ["organic", "fair_trade"]),
    # two processes, two regions, feet, a harvest year only, an unmodeled certification: all abstain
    ("70801", None, "Costa Rica", None, None, None, (None, None), None, None),
    # two lot numbers: abstain; anaerobic wins over natural
    (None, "Lakeside", "Brazil", "Mogiana", "anaerobic", ["Yellow Bourbon"], (1100, 1100),
     ["jun", "jul", "aug", "sep"], None),
    # not a lot at all
    (None, None, None, None, None, None, (None, None), None, None),
]  # fmt: skip


@pytest.mark.parametrize("i", range(len(PRODUCTS)))
def test_rules_on_the_synthetic_products(i):
    cells = importers.parse_royal(PRODUCTS, LISTING).lots[i].cells
    problems: list[str] = []
    got = importers.stated("royal_coffee", cells, problems) | rules.normalize("royal_coffee", cells, problems)
    lot, wh, origin, region, proc, var, (lo, hi), harvest, certs = EXPECTED[i]
    assert problems == []
    assert {
        k: got.pop(k) for k in ("name", "process", "status", "est_arrival", "bags", "bag_size", "bag_kg")
    } == {
        "name": PRODUCTS[i]["title"],
        "process": {0: "Washed", 2: "Decaf (Sugarcane EA)"}.get(i),  # the label, as stated
        "status": None,
        "est_arrival": None,
        "bags": None,
        "bag_size": None,
        "bag_kg": None,
    }
    assert got == {
        "lot_id": lot,
        "warehouse": wh,
        "origin": origin,
        "region": region,
        "process_type": proc,
        "varieties": var,
        "altitude_min_m": lo,
        "altitude_max_m": hi,
        "harvest_months": harvest,
        "grower_type": None,
        "grower_name": None,
        "certifications": certs,
        "not_certified": None,  # the store never marks one absent: unknown, never "not certified"
        "rules_version": rules.ROYAL_RULES_VERSION,
    }


def _cells(title: str = "Invented Lot", tags: list[str] = (), body: str = "") -> dict[str, str | None]:  # type: ignore[assignment]
    return {"title": title, "tags": "\n".join(tags) or None, "body_text": body or None, "product_type": None}


@pytest.mark.parametrize(
    "cells, field, expected",
    [
        # the review's cases on #140's rules, here as Royal labels: no crash, no confident wrong value
        (_cells(body="Altitude: 1.2.3 m"), "altitude", (None, None)),
        (_cells(body="Altitude: Bags of 1,000 m"), "altitude", (None, None)),
        (_cells(body="This harvest declined because of rain"), "harvest", None),
        (_cells(body="Harvest: Dec"), "harvest", ["dec"]),
        (_cells(body="Harvest: may be smaller this year"), "harvest", None),  # the verb, not the month
        (_cells(body="Harvest: May"), "harvest", ["may"]),
        (_cells("Mexico Natural", body="Processed with mountain water decaf method"), "process", None),
        (_cells("Rwanda Washed", ["Natural"]), "process", None),  # title and tag disagree
        (_cells("Indonesia Wet-Hulled"), "process", "honey"),
        (_cells("Brazil Pulped Natural"), "process", "honey"),
        (_cells(tags=["Process: Semi-Washed"]), "process", "honey"),
        (_cells(tags=["Variety: Java"]), "varieties", ["Java"]),
        (_cells(tags=["Variety: Bourbon, Mystery Hybrid"]), "varieties", None),
        (_cells(body="Varieties grown nearby include Gesha"), "varieties", None),  # prose, not a label
        (_cells(tags=["Certification: C.A.F.E. Practices"]), "certifications", None),
        (_cells("Honduras Organic"), "certifications", ["organic"]),
        (_cells(tags=["Organic coffee lovers will enjoy"]), "certifications", None),  # a tag that says more
        (_cells("Lot 123 - Lot 123"), "lot", "123"),
        (_cells("Lot 12 sample"), "lot", None),
        (_cells("Kenya AA - North Warehouse", ["Warehouse: South"]), "warehouse", None),
        (_cells(tags=["Origin: Sumatra"]), "origin", "Indonesia"),
        (_cells("Colombia and Peru Blend"), "origin", None),
    ],
)
def test_rules_abstain_when_unsure(cells, field, expected):
    fn = {
        "altitude": rules.royal_altitude,
        "harvest": rules.royal_harvest_months,
        "process": rules.royal_process,
        "varieties": rules.royal_varieties,
        "certifications": rules.royal_certifications,
        "lot": lambda c: rules.royal_lot(c["title"]),
        "warehouse": rules.royal_warehouse,
        "origin": rules.royal_origin,
    }[field]
    assert fn(cells) == expected
