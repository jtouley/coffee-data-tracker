"""Royal Coffee, the third importer (coffeedata.importers, importer_rules): its Shopify listing read page by page,
each product kept word for word without prices or images, raw-only and immutable per week, its values derived when
read, and no network call to the store from any test or CI job.

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
WED, MON = date(2026, 11, 11), date(2026, 11, 9)
NOW = datetime(2026, 11, 11, 17, 37, 5, tzinfo=UTC)
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
        rows = importers.read_royal(client, LISTING)
    assert len(rows) == 6
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
        assert len(importers.read_royal(client, LISTING)) == 6
    assert [c.url.params.get("page") for c in calls if c.url.path != "/robots.txt"] == ["1", "2", "3"]

    calls.clear()  # a store that ignores `page`, answering the first page every time
    with _store([PRODUCTS[:3]] * 50, calls) as client:
        assert len(importers.read_royal(client, LISTING)) == 3
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
        cli.main(["importer-snapshot", "--run-date", "2026-11-11"])
    except SystemExit as e:
        assert e.code in (0, None)
    finally:
        importers.snapshot = orig
    assert seen["interval"] == importers.ROYAL_PAGE_INTERVAL >= 10


def test_each_product_is_kept_word_for_word_without_prices_or_images():
    rows = importers.parse_royal(PRODUCTS, LISTING)
    assert [r["row"] for r in rows] == list(range(6))
    first = rows[0]["_raw"]
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
    assert rows[0]["_url"] == f"{FAKE}/products/{p['handle']}"
    for r in rows:
        stored = json.dumps(r["_raw"])
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
    with pytest.raises(importers.LayoutChanged, match="zero rows"):
        importers.parse_royal([], LISTING)
    unreadable = [{k: v for k, v in p.items() if k != "handle"} for p in PRODUCTS[:2]] + PRODUCTS[2:]
    with pytest.raises(importers.LayoutChanged, match="unreadable"):
        importers.parse_royal(unreadable, LISTING)
    calls: list[httpx.Request] = []
    with _store([], calls) as client:
        client_get = client.fetch_json
        client.fetch_json = lambda url, **kw: {"items": []}  # type: ignore[method-assign]
        with pytest.raises(importers.LayoutChanged, match="not a /products.json"):
            importers.read_royal(client, LISTING)
        client.fetch_json = client_get  # type: ignore[method-assign]


def _with_fake_store(monkeypatch):
    royal = dataclasses.replace(importers.IMPORTERS[importers.ROYAL], locate=lambda: LISTING)
    monkeypatch.setitem(importers.IMPORTERS, importers.ROYAL, royal)


def test_raw_only_one_immutable_file_a_week_and_values_derived_when_read(tmp_path, monkeypatch):
    _with_fake_store(monkeypatch)
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    calls: list[httpx.Request] = []
    with _store(_pages_of(250), calls) as client:
        res = importers.snapshot(lake, client, [importers.ROYAL], today=WED, now=lambda: NOW)
    assert res == [{"importer": "royal_coffee", "week": "2026-11-09", "status": "stored", "rows": 6}]
    raw_path = tmp_path / "importers" / importers.path(importers.ROYAL, MON, "importer_raw")
    assert not (tmp_path / "importers" / importers.path(importers.ROYAL, MON)).exists()  # no offers file
    raw = pq.read_table(raw_path).to_pylist()
    assert {(r["importer"], r["source_url"], r["week"]) for r in raw} == {("royal_coffee", LISTING, MON)}
    assert [r["url"] for r in raw] == [f"{FAKE}/products/{p['handle']}" for p in PRODUCTS]
    # derived from the stored raw cells, never stored
    first = importers.derive(raw[0])
    assert first["lot_id"] == "70412" and first["process_type"] == "washed" and first["week"] == MON

    calls.clear()  # later that week: not fetched again, not rewritten
    with _store(_pages_of(250), calls) as client:
        again = importers.snapshot(lake, client, [importers.ROYAL], today=date(2026, 11, 14), now=lambda: NOW)
    assert again[0]["status"] == "skipped" and calls == []
    with pytest.raises(FileExistsError):
        lake.write("importer_raw", importers.path(importers.ROYAL, MON, "importer_raw"), raw)


# --- no test and no CI job ever reaches Royal -----------------------------------------------------------------------


def test_without_its_address_royal_is_skipped_and_nothing_is_fetched(tmp_path):
    calls: list[httpx.Request] = []
    lake = Lake(str(tmp_path / "importers"), tables=IMPORTER_TABLES)
    with _store([], calls) as client:
        res = importers.snapshot(lake, client, [importers.ROYAL], today=WED, now=lambda: NOW)
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
            today=WED,
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
    cells = importers.parse_royal(PRODUCTS, LISTING)[i]["_raw"]
    got = rules.normalize("royal_coffee", cells)
    lot, wh, origin, region, proc, var, (lo, hi), harvest, certs = EXPECTED[i]
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
        "certifications": certs,
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
