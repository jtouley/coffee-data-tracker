"""The usage reports (dbt_usage/, usage_build.py, geo.py; design sections 0, 3-6): built on a synthetic raw usage
dataset and a tiny synthetic geo database (no real events exist yet, and the real geo files are too large for a
test). Covers the pinned geo lock and its sha check, the location lookup, the thresholds and city floors (each made
to fail on a deliberately bad row), the detector and its alert text, idempotency, and that nothing from the usage
project reaches anything public."""

from __future__ import annotations

import ast
import inspect
import json
import re
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from coffeedata import geo, usage_build

from .lakehelp import needs_dbt

REPO = Path(__file__).resolve().parent.parent
FIRST = date(2026, 9, 7)  # a Monday; the synthetic traffic runs 8 weeks from here
DAYS = 56
CANARY_IP_PREFIX = "203.0.113.0/24"  # where the collector would have masked 203.0.113.77
CANARY_TOWN = "Canary Gulch"  # an invented town in the test geo file, too small for the city table
CANARY_EMAIL = "canary@example.com"
HARVEST_SEARCHES = ("gesha 2023 2024", "2022 2023 2024 2025", "kenya aa 2024", "decaf 1/2 lb 12 oz 2025")

# The synthetic geo database: ranges as (first, last, country, region, city), and the city table.
GEO_RANGES = [
    ("192.0.2.0", "192.0.2.255", "US", "Colorado", "Boulder"),
    ("198.51.100.0", "198.51.100.127", "US", "Colorado", "Denver (Five Points)"),
    ("198.51.100.128", "198.51.100.255", "US", "Colorado", "Denver"),
    # a /24 split between two cities: its events are only ever Colorado
    ("100.64.0.0", "100.64.0.127", "US", "Colorado", "Denver"),
    ("100.64.0.128", "100.64.0.255", "US", "Colorado", "Aurora"),
    ("203.0.113.0", "203.0.113.255", "US", "Colorado", CANARY_TOWN),
    ("2001:db8::", "2001:db8:ff:ffff:ffff:ffff:ffff:ffff", "US", "Oregon", "Portland"),
]
GEO_CITIES = [
    ("US", "Colorado", "denver", 715_000),
    ("US", "Colorado", "boulder", 108_000),
    ("US", "Colorado", "aurora", 390_000),
    ("US", "Oregon", "portland", 650_000),
]


# --- the synthetic dataset ------------------------------------------------------------------------------------------


def _event(n: int, day: date, hour: int, event: str, ip: str, **kw) -> dict:
    base = {
        "event_id": f"e{n:06d}",
        "received_at": datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
        "event": event,
        "surface": "catalog",
        "release": "r1",
        "device": "desktop",
        "query": None,
        "cities": [],
        "states": [],
        "origin": None,
        "process": None,
        "state": None,
        "in_stock": None,
        "sort": None,
        "results_n": None,
        "zero_results": None,
        "roaster_id": None,
        "coffee_id": None,
        "rank": None,
        "source": None,
        "referrer_domain": None,
        "utm_source": None,
        "utm_medium": None,
        "utm_campaign": None,
        "ip_prefix": ip,
        "bot": False,
    }
    return base | kw


def synthetic_events() -> list[dict]:
    """Eight weeks of invented traffic, built so each rule has a case on both sides of its line."""
    out: list[dict] = []
    n = 0

    def add(day, hour, event, ip, **kw):
        nonlocal n
        n += 1
        out.append(_event(n, day, hour, event, ip, **kw))

    for i in range(DAYS):
        d = FIRST + timedelta(days=i)
        # Denver: every day, 4 events: a named city (big, every day, 28 a week)
        add(d, 9, "page_view", "198.51.100.0/24", source="direct")
        add(
            d,
            9,
            "search",
            "198.51.100.0/24",
            query="Ethiopia",
            origin="Ethiopia",
            results_n=12,
            zero_results=False,
        )
        add(d, 9, "click_out", "198.51.100.0/24", roaster_id="r.denver", coffee_id="c1", rank=1, query="Ethiopia",
            origin="Ethiopia")  # fmt: skip
        add(d, 10, "page_view", "2001:db8::/40", surface="landing", device="phone",
            source="utm", utm_source="newsletter", utm_medium="email", utm_campaign="fall")  # fmt: skip
        # the canary town: every day, many events, but a town of no recorded size: only ever "other, Colorado"
        for h in range(4):
            add(
                d, 12 + h, "filter_state", CANARY_IP_PREFIX, process="washed", results_n=3, zero_results=False
            )
        # Boulder: big enough, but only on Mondays (8 days in 8 weeks, under the 10-day floor)
        if d.weekday() == 0:
            for _ in range(25):
                add(d, 14, "page_view", "192.0.2.0/24", source="referrer", referrer_domain="news.example.org")
        # a query with a word outside the vocabulary, on 4 days: under the 5-day bar for unknown words
        if i in (1, 2, 3, 4):
            add(d, 15, "search", "198.51.100.0/24", query="xyzzy blend", results_n=0, zero_results=True)
        # a known query on 3 days in week 1: passes at 3
        if i in (0, 1, 2):
            add(d, 15, "search", "198.51.100.0/24", query="  KENYA ", cities=["Denver"], states=["CO"])
        # Denver on phones, 4 a week: under the cell floor, so the device facet is held back for Denver (else
        # phone = total - desktop)
        if d.weekday() in (1, 3):
            for _ in range(2):
                add(d, 17, "page_view", "198.51.100.0/24", device="phone")
        # from the split /24, every day: Colorado, never a city
        add(d, 18, "page_view", "100.64.0.0/24")
        # bots, every day: never counted
        add(d, 16, "search", "198.51.100.0/24", query="botquery", bot=True)
    # the canary search (and a phone number), once each: the detector fires with counts only
    add(FIRST + timedelta(days=5), 11, "search", CANARY_IP_PREFIX, query=CANARY_EMAIL)
    add(FIRST + timedelta(days=5), 11, "search", CANARY_IP_PREFIX, query="call 303-555-0100")
    # more phone formats, and a test card number: each counts once
    for q in ("(303) 555-0199", "+44 20 7946 0958", "303.555.0123", "4111 1111 1111 1111"):
        add(FIRST + timedelta(days=5), 11, "search", CANARY_IP_PREFIX, query=q)
    # ordinary searches with numbers in them: harvest years, side by side, are never a phone or a card
    for q in HARVEST_SEARCHES:
        add(FIRST + timedelta(days=5), 11, "search", CANARY_IP_PREFIX, query=q)
    # a referrer seen on one day only
    add(FIRST + timedelta(days=6), 11, "page_view", "198.51.100.0/24", source="referrer",
        referrer_domain="once.example")  # fmt: skip
    return out


def write_dataset(root: Path, events: list[dict], reverse: bool = False) -> None:
    """Raw as the collector stores it: events/day=YYYY-MM-DD/batch=<id>.parquet, plus one retried batch (the same
    event ids again) so dedup is exercised."""
    by_day: dict[str, list[dict]] = {}
    for e in events:
        by_day.setdefault(e["received_at"].date().isoformat(), []).append(e)
    for day, rows in sorted(by_day.items(), reverse=reverse):
        folder = root / "events" / f"day={day}"
        folder.mkdir(parents=True, exist_ok=True)
        rows = list(reversed(rows)) if reverse else rows
        pq.write_table(pa.Table.from_pylist(rows), folder / "batch=a.parquet")
    retry = events[:3]
    pq.write_table(
        pa.Table.from_pylist(retry), root / "events" / f"day={FIRST.isoformat()}" / "batch=retry.parquet"
    )
    # a copy of the first event received an hour earlier, in a batch of its own: dedup keeps the earliest
    early = events[0] | {"received_at": events[0]["received_at"] - timedelta(hours=1)}
    pq.write_table(
        pa.Table.from_pylist([early]), root / "events" / f"day={FIRST.isoformat()}" / "batch=early.parquet"
    )


def write_geo(root: Path, lock_path: Path, ranges=GEO_RANGES, cities=GEO_CITIES) -> dict:
    """A tiny geo database in the dataset's geo/ folder, prepared in the same shape as geo.py's, and its lock."""
    (root / "geo").mkdir(parents=True, exist_ok=True)
    rows = sorted((geo.to_hex(a), geo.to_hex(b), c, r, t) for a, b, c, r, t in ranges)
    pq.write_table(pa.Table.from_pylist(
        [dict(zip(geo.RANGE_SCHEMA.names, r, strict=True)) for r in rows], schema=geo.RANGE_SCHEMA),
        root / "geo/dbip-test.parquet")  # fmt: skip
    pq.write_table(pa.Table.from_pylist(
        [dict(zip(geo.CITY_SCHEMA.names, r, strict=True)) for r in cities], schema=geo.CITY_SCHEMA),
        root / "geo/cities-test.parquet")  # fmt: skip
    entries = {
        geo.DBIP: {"file": "geo/dbip-test.parquet", "release": "2026-11-01",
                   "sha256": geo.sha256(root / "geo/dbip-test.parquet")},
        geo.CITIES: {"file": "geo/cities-test.parquet", "release": "2026-11-13",
                     "sha256": geo.sha256(root / "geo/cities-test.parquet")},
    }  # fmt: skip
    lock_path.write_text(geo.render_lock(entries))
    return entries


def write_catalog(root: Path) -> dict[str, str]:
    """A served release's dim_roaster and dim_coffee, the few columns the usage build reads."""
    (root / "release").mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([
        {"roaster_id": "r.denver", "roaster_name": "Mile High Roasters", "city": "Denver", "state": "CO"}]),
        root / "release/dim_roaster.parquet")  # fmt: skip
    pq.write_table(pa.Table.from_pylist([
        {"coffee_id": "c1", "title": "Ethiopia Guji", "origin_country": "Ethiopia", "process": "Washed"},
        {"coffee_id": "c2", "title": "Kenya AA", "origin_country": "Kenya", "process": "Washed"}]),
        root / "release/dim_coffee.parquet")  # fmt: skip
    return {"dim_roaster": str(root / "release/dim_roaster.parquet"),
            "dim_coffee": str(root / "release/dim_coffee.parquet")}  # fmt: skip


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """Each test's private database and geo cache live under its own tmp_path (usage_build.ROOT)."""
    monkeypatch.setattr(usage_build, "ROOT", tmp_path.resolve())
    monkeypatch.delenv("COFFEEDATA_DB", raising=False)
    monkeypatch.delenv("PUBLIC_EXPORT_URL", raising=False)
    return tmp_path


def _build(checkout: Path, events=None, reverse=False, target=None, tz=None, monkeypatch=None) -> Path:
    data = checkout / "usage"
    write_dataset(data, synthetic_events() if events is None else events, reverse=reverse)
    lock = checkout / "geo.lock"
    if not lock.exists():
        write_geo(data, lock)
    catalog = write_catalog(checkout)
    target = target or checkout / "target"
    log = checkout / "dbt.log"
    if tz and monkeypatch:
        monkeypatch.setenv("TZ", tz)
    ok = usage_build.build(
        str(data), lock_path=lock, catalog=catalog, today="2026-11-02", target=target, log=log
    )
    return checkout / usage_build.DB, ok, target, log


def _rows(con, table: str) -> list[tuple]:
    return con.execute(f"select * from {table} order by all").fetchall()


# --- the geo lock ---------------------------------------------------------------------------------------------------


def test_the_lock_names_each_file_and_its_sha_and_the_build_refuses_another(checkout):
    lock = geo.read_lock()  # the real lock: both files, under geo/, each with a release and a sha256
    for key in (geo.DBIP, geo.CITIES):
        assert lock[key]["file"].startswith("geo/") and re.fullmatch(r"[0-9a-f]{64}", lock[key]["sha256"])
        assert lock[key]["licence"] == "CC BY 4.0"
    data = checkout / "usage"
    entries = write_geo(data, checkout / "geo.lock")
    local = geo.locate(
        geo.read_lock(checkout / "geo.lock"), checkout / "cache", usage_build.fetcher(str(data))
    )
    assert geo.sha256(local[geo.DBIP]) == entries[geo.DBIP]["sha256"]
    # a cached file that isn't the locked one stops the build before dbt runs
    local[geo.CITIES].write_bytes(b"not the locked file")
    with pytest.raises(ValueError, match="is not the locked"):
        geo.locate(geo.read_lock(checkout / "geo.lock"), checkout / "cache", usage_build.fetcher(str(data)))
    # the build's own cache (usage_build.GEO_CACHE), the same way
    geo.locate(
        geo.read_lock(checkout / "geo.lock"), checkout / usage_build.GEO_CACHE, usage_build.fetcher(str(data))
    )
    (checkout / usage_build.GEO_CACHE / entries[geo.CITIES]["file"]).write_bytes(b"not the locked file")
    with pytest.raises(ValueError, match="is not the locked"):
        _build(checkout)
    assert not (checkout / usage_build.DB).exists()


def test_a_lock_entry_must_be_complete_and_under_geo(tmp_path):
    p = tmp_path / "geo.lock"
    p.write_text('[dbip_city_lite]\nfile = "x.parquet"\nrelease = "1"\nsha256 = "a"\n[geonames_cities]\n')
    with pytest.raises(ValueError):
        geo.read_lock(p)


def test_prepared_files_are_byte_for_byte_reproducible(tmp_path):
    """geo-update on one download gives the same bytes, so the lock's sha256 can be checked by anyone re-preparing."""
    import gzip

    csv = tmp_path / "dbip.csv.gz"
    with gzip.open(csv, "wt") as f:
        f.write(
            '1.0.0.0,1.0.0.127,OC,AU,Queensland,"South Brisbane",0,0\n1.0.0.128,1.0.0.255,OC,AU,Victoria,X,0,0\n'
            "2001:db8::,2001:db8::ffff,NA,US,Oregon,Portland,0,0\n0.0.0.0,0.255.255.255,ZZ,ZZ,,,0,0\n"
        )
    a = geo.prepare_dbip(csv, tmp_path / "a.parquet")
    geo.prepare_dbip(csv, tmp_path / "b.parquet")
    assert geo.sha256(tmp_path / "a.parquet") == geo.sha256(tmp_path / "b.parquet")
    assert (
        a["prefix_accuracy"]["ipv4_24"]["blocks_with_more_than_one_place"] == 1
    )  # 1.0.0.0/24 has two places
    assert (
        a["prefix_accuracy"]["ipv4_24"]["ranges_moved"] == 1
    )  # the second half looks up the first half's city
    rows = pq.read_table(tmp_path / "a.parquet").to_pylist()
    assert rows[0]["country"] is None  # ZZ is no country
    assert rows[-1]["ip_start"] == geo.to_hex("2001:db8::") or rows[0]["ip_start"] == geo.to_hex("0.0.0.0")


# --- the build ------------------------------------------------------------------------------------------------------


@needs_dbt
def test_the_usage_build_on_synthetic_raw(checkout):
    """Location, thresholds, city floors, bots and dedup, on the synthetic dataset; then every guard and the
    detector."""
    db, ok, target, log = _build(checkout)
    text = log.read_text()
    # the detector fails the build (the canary email and the phone number), and nothing else does
    assert not ok
    assert re.search(r"FAIL \d+ search_text_looks_personal", text) or "search_text_looks_personal" in text
    results = json.loads((target / "run_results.tests.json").read_text())["results"]
    failed = {r["unique_id"].split(".")[-1] for r in results if r["status"] in ("fail", "error")}
    assert failed == {"search_text_looks_personal"}, failed
    models = json.loads((target / "run_results.models.json").read_text())["results"]
    assert models and {r["status"] for r in models} == {"success"}
    counts = usage_build.run_counts(target)
    assert counts["model"] == {"success": len(usage_build.models())} and counts["test"]["fail"] == 1
    assert CANARY_EMAIL not in text and "303-555-0100" not in text  # dbt's output never shows a search
    con = duckdb.connect(str(db), read_only=True)
    try:
        # dedup: the retried batch adds nothing
        assert con.execute("select count(*) from stg_usage_events").fetchone()[0] == len(synthetic_events())
        # ... and keeps the earliest copy of an event
        first = synthetic_events()[0]
        assert con.execute("select received_at from stg_usage_events where event_id = ?", [first["event_id"]]
                           ).fetchone()[0] == first["received_at"].replace(tzinfo=None) - timedelta(hours=1)  # fmt: skip
        # secondary suppression: Denver's device facet (4 phone events a week) is held back whole
        assert not con.execute(
            "select count(*) from mart_usage_city_facets_weekly where city = 'Denver' and facet = 'device'"
        ).fetchone()[0]
        assert con.execute(
            "select count(*) from mart_usage_city_facets_weekly where city = 'Denver' and facet = 'surface'"
        ).fetchone()[0]
        # location: IPv4 /24 and IPv6 /40 prefixes both found
        loc = dict(con.execute(
            "select l.city, count(*) from int_usage_location l group by 1").fetchall())  # fmt: skip
        assert loc["Denver"] > 0 and loc["Portland"] > 0 and loc[CANARY_TOWN] > 0
        # the split /24 resolves to its region only (consensus), and "Denver (Five Points)" to Denver
        assert (
            con.execute(
                "select count(*) from int_usage_location where region = 'Colorado' and city is null"
            ).fetchone()[0]
            == DAYS
        )
        assert "Denver (Five Points)" not in loc and "Aurora" not in loc
        # city floors: Denver (every day, 21+ a week) named; Boulder (8 days), Portland (7 events a week, under 20)
        # and the canary town (no population) are not
        cities = {
            r[0] for r in con.execute("select distinct city from mart_usage_locations_weekly").fetchall()
        }
        assert {"Denver", "other, Colorado", "other, Oregon"} <= cities
        assert not {"Boulder", "Portland", CANARY_TOWN} & cities
        # Aurora never had an event, so it never shows
        assert "Aurora" not in cities
        # thresholds: 'ethiopia' (every day) and 'kenya' (3 days, known words) shown; 'xyzzy blend' (4 days, an
        # unknown word) is rare; bots never counted
        queries = {
            r[0] for r in con.execute("select distinct query from mart_usage_search_terms_weekly").fetchall()
        }
        assert {"ethiopia", "kenya", "(rare)"} <= queries
        assert "xyzzy blend" not in queries and "botquery" not in queries and CANARY_EMAIL not in queries
        # sources: the campaign and the referrer domain with enough days, the direct visits
        srcs = set(
            con.execute("select source_kind, source, utm_campaign from mart_usage_sources_weekly").fetchall()
        )
        assert ("utm", "newsletter", "fall") in srcs and ("direct", "direct", "none") in srcs
        assert ("referrer", "news.example.org", "none") in srcs  # Mondays: 3 days by week 3
        # clicks: by roaster with a region and never a city; context with the query behind them
        assert con.execute("select sum(click_outs) from mart_roaster_clicks_weekly").fetchone()[0] == DAYS
        assert ("query", "ethiopia") in set(
            con.execute("select context_kind, context_value from mart_click_context_weekly").fetchall()
        )
        # the geo lock is recorded
        assert {r[0] for r in con.execute("select geo_key from ops_usage_build").fetchall()} == {
            geo.DBIP,
            geo.CITIES,
        }
    finally:
        con.close()
    # the canary: the masked prefix is only in raw, never in a table of the database; the town only rolled up
    with duckdb.connect(str(db), read_only=True) as con:
        tables = [
            t
            for (t,) in con.execute(
                "select table_name from information_schema.tables where table_type = 'BASE TABLE'"
            ).fetchall()
        ]
        for t in tables:
            dump = json.dumps(con.execute(f"select * from {t}").fetchall(), default=str)
            assert "203.0.113" not in dump, t
            if t.startswith("mart_"):
                assert CANARY_TOWN not in dump and CANARY_EMAIL not in dump, t
    # the detector's counts and the alert's text: counts and patterns only
    counts = usage_build.detector_counts(db, target)
    # 4 phone formats, 1 Luhn-valid card, 1 email; the harvest years count as nothing
    assert counts == [("2026-09-07", "card", 1), ("2026-09-07", "email", 1), ("2026-09-07", "phone", 4)]
    alert = usage_build.detector_alert(counts)
    assert alert["firing"] and alert["summary"] == (
        "1 searches in week 2026-09-07 matched the card pattern; 1 searches in week 2026-09-07 matched the email "
        "pattern; 4 searches in week 2026-09-07 matched the phone pattern"
    )
    assert not any(q in text for q in HARVEST_SEARCHES[:2])
    from coffeedata import triage

    body = triage.render(alert, "now")
    assert CANARY_EMAIL not in body and "303-555" not in body and "canary" not in body.lower()
    assert "1 searches in week 2026-09-07 matched the email pattern" in body
    quiet = usage_build.detector_alert([])
    assert not quiet["firing"] and {c["status"] for c in quiet["checks"]} == {"ok"}


def _dbt_test(checkout: Path, target: Path, select: str) -> subprocess.CompletedProcess:
    lock = geo.read_lock(checkout / "geo.lock")
    local = geo.locate(lock, checkout / usage_build.GEO_CACHE)
    variables = usage_build.dbt_vars(
        str(checkout / "usage"), lock, local, write_catalog(checkout), "2026-11-02"
    )
    project = str(usage_build.PROJECT)
    env = {k: v for k, v in __import__("os").environ.items() if k != "COFFEEDATA_DB"}
    env["COFFEEDATA_USAGE_DB"] = usage_build.DB
    return subprocess.run(
        ["dbt", "test", "--select", select, "--project-dir", project, "--profiles-dir", project,
         "--target-path", str(target), "--vars", json.dumps(variables)],
        cwd=checkout, env=env, capture_output=True, text=True,
    )  # fmt: skip


# Each guard with the bad row it exists to stop, written into the built table it reads: a guard that stopped
# selecting anything (a renamed column, a dropped ref) would show here.
GUARD_BREAKS = {
    # a query shown after 1 day
    "report_rows_meet_threshold": "insert into mart_usage_search_terms_weekly values "
    "(date '2026-09-07', 'xyzzy blend', 1, 1)",
    # an unknown-word query shown after 4 days (the bar is 5)
    "report_rows_meet_threshold#unknown": "update mart_usage_search_terms_weekly set query = 'xyzzy blend' "
    "where query = '(rare)' and week = date '2026-09-07'",
    # a campaign tag shown below its days
    "report_rows_meet_threshold#campaign": "insert into mart_usage_sources_weekly values "
    "(date '2026-09-07', 'campaign', 'newsletter', 'email', 'secret-test', 1)",
    # a town under 50,000 named
    "cities_meet_floors": f"update mart_usage_locations_weekly set city = '{CANARY_TOWN}' "
    "where city = 'other, Colorado'",
    # a big city below the day floor (Boulder: 8 days)
    "cities_meet_floors#days": "insert into mart_usage_locations_weekly values "
    "(date '2026-10-26', 'US', 'Colorado', 'Boulder', 25, 25)",
    # a city x facet cell under 10 events
    "cities_meet_floors#cell": "insert into mart_usage_city_facets_weekly values "
    "(date '2026-09-07', 'US', 'Colorado', 'Denver', 'device', 'tablet', 3)",
    # a city beside a query
    "no_city_with_query_or_roaster": "alter table mart_usage_locations_weekly add column query varchar",
    # a city beside a roaster
    "no_city_with_query_or_roaster#roaster": "alter table mart_roaster_clicks_weekly add column city varchar",
    # a second facet beside the city
    "no_city_with_query_or_roaster#facet": "update mart_usage_city_facets_weekly set facet = 'query' "
    "where facet = 'device'",
    # the reviewer's probes: a visitor city under another name beside a roaster
    "no_city_with_query_or_roaster#place_city": "alter table mart_roaster_clicks_weekly add column place_city varchar",
    # a referrer seen on one day as a roaster's click source
    "report_rows_meet_threshold#roaster_source": "update mart_roaster_clicks_weekly set source = 'once.example' "
    "where week = date '2026-09-07'",
    # a rare source as a Denver facet cell of 12 events
    "report_rows_meet_threshold#facet_source": "insert into mart_usage_city_facets_weekly values "
    "(date '2026-09-14', 'US', 'Colorado', 'Denver', 'source', 'once.example', 12)",
    # the subtraction case: Denver's desktop cell shown alone (what the first version published) gives phone = 4
    "cities_meet_floors#subtraction": "insert into mart_usage_city_facets_weekly select week, country, region, city, "
    "'device', 'desktop', events - 4 from mart_usage_locations_weekly where city = 'Denver'",
    # a place's page views shown beside a small remainder
    "cities_meet_floors#page_views": "update mart_usage_locations_weekly set page_views = events - 3 "
    "where city = 'Denver'",
    # the prefix leaking into a mart
    "ip_prefix_only_in_location": "alter table mart_usage_locations_weekly add column ip_prefix varchar",
}


@needs_dbt
def test_each_threshold_and_city_rule_fails_on_a_bad_row(checkout):
    db, _ok, target, _log = _build(checkout)
    clean = checkout / "clean.duckdb"
    shutil.copy(db, clean)
    for case, break_it in GUARD_BREAKS.items():
        guard = case.split("#")[0]
        good = _dbt_test(checkout, target, guard)
        assert good.returncode == 0 and "ERROR=0 " in good.stdout, (case, good.stdout[-2000:])
        with duckdb.connect(str(db)) as con:
            con.execute(break_it)
        bad = _dbt_test(checkout, target, guard)
        assert bad.returncode != 0 and "ERROR=1 " in bad.stdout and " FAIL " in bad.stdout, (
            case,
            bad.stdout[-2000:],
        )
        assert (
            "secret-test" not in bad.stdout and CANARY_TOWN not in bad.stdout
        )  # failures show counts, not rows
        shutil.copy(clean, db)


@needs_dbt
def test_two_builds_of_the_same_raw_and_lock_have_the_same_rows(checkout, tmp_path_factory, monkeypatch):
    """Idempotency (docs/dbt/CONVENTION.md section 2): the second build reads every file's rows, and every folder's
    files, in reverse order, in another time zone; every table and view has the same rows."""
    db, _ok, _t, _l = _build(checkout)
    first = checkout / "first.duckdb"
    shutil.copy(db, first)
    shutil.rmtree(checkout / "usage" / "events")
    db2, _ok2, _t2, _l2 = _build(checkout, reverse=True, target=checkout / "target2", tz="Asia/Tokyo",
                                 monkeypatch=monkeypatch)  # fmt: skip
    a = duckdb.connect(str(first), read_only=True)
    b = duckdb.connect(str(db2), read_only=True)
    try:
        for con in (a, b):
            con.execute("set TimeZone = 'UTC'")
        tables = sorted(
            t for (t,) in a.execute("select table_name from information_schema.tables").fetchall()
        )
        assert tables == sorted(
            t for (t,) in b.execute("select table_name from information_schema.tables").fetchall()
        )
        assert "mart_usage_locations_weekly" in tables
        for t in tables:
            if t == "stg_usage_events" or t.startswith("stg_usage_ip"):
                continue  # views over raw: compared through what is built from them
            assert _rows(a, t) == _rows(b, t), t
        assert _rows(a, "stg_usage_events") == _rows(b, "stg_usage_events")
    finally:
        a.close()
        b.close()


# --- out of every public output -------------------------------------------------------------------------------------


def _manifest(project: Path, target: Path) -> dict:
    subprocess.run(
        ["dbt", "parse", "--project-dir", str(project), "--profiles-dir", str(project), "--target-path", str(target)],
        check=True, capture_output=True, env={**__import__("os").environ, "COFFEEDATA_USAGE_DB": usage_build.DB,
                                              "COFFEEDATA_DB": "build/coffee.duckdb"},
        cwd=REPO,
    )  # fmt: skip
    return json.loads((target / "manifest.json").read_text())


@needs_dbt
def test_usage_models_never_reach_a_public_model_export_or_share(tmp_path):
    """The lineage test (design section 4): the usage models are a project of their own, so no public_* model,
    served mart, release table, app export or share descends from them. Checked on both projects' manifests: the
    main one has no node, source or macro from the usage project, and the usage one has no public, served or
    exported name and nothing with access public; its marts have enforced contracts (private)."""
    from coffeedata import cli, public, quality

    main = _manifest(REPO / "dbt", tmp_path / "main")
    usage = _manifest(usage_build.PROJECT, tmp_path / "usage")
    usage_models = {n["name"] for n in usage["nodes"].values() if n["resource_type"] == "model"}
    assert usage_models == usage_build.models() and len(usage_models) >= 15
    published = (
        set(public.PUBLIC_TABLES) | set(quality.SERVED_MARTS) | set(quality.RELEASE_TABLES) | set(cli.MARTS)
    )
    assert not usage_models & published
    assert not any(m.startswith("public_") for m in usage_models)
    for n in usage["nodes"].values():
        if n["resource_type"] == "model":
            assert n["config"].get("access", "protected") != "public", n["name"]
            # the marts the owner reads have enforced contracts, but are never public
            assert n["config"].get("contract", {}).get("enforced") == n["name"].startswith("mart_"), n["name"]
    main_nodes = {n["name"] for n in main["nodes"].values()}
    assert not main_nodes & usage_models
    assert all(n["package_name"] == "coffeedata" for n in main["nodes"].values())
    main_text = json.dumps(main).lower()
    for word in (
        "coffee-usage",
        "usage_sources",
        "geo_db",
        "dbt_usage",
        "usage_raw",
        "stg_usage",
        "int_usage",
    ):
        assert word not in main_text, word


def test_nothing_public_names_the_usage_project_its_database_or_dataset():
    """The main dbt project, the contracts, the site's templates, the public export, Snowflake and the MCP server never
    name the usage project, its database, its dataset or its tables; only usage_build.py and the CLI's two usage
    commands do."""
    from coffeedata import importers

    names = ("dbt_usage", "usage.duckdb", "coffee-usage", "coffeedata_usage", "usage_build", "coffeedata_usage_db",
             *usage_build.models())  # fmt: skip
    sources = [
        *(REPO / "dbt").rglob("*.sql"),
        *(REPO / "dbt").rglob("*.yml"),
        *(REPO / "dbt").rglob("*.py"),
        *(REPO / "contracts").glob("*.yaml"),
        *(REPO / "marketplace").rglob("*.*"),
        *(REPO / "src/coffeedata/templates").rglob("*.*"),
        *(REPO / "src/coffeedata" / f for f in ("public.py", "site.py", "snowflake_load.py", "mcp_server.py",
                                                "quality.py", "lake.py")),
    ]  # fmt: skip
    skip = {"__pycache__", "target", "logs", "dbt_packages"}
    for f in sources:
        if f.is_file() and not skip & set(f.relative_to(REPO).parts):
            text = f.read_text(errors="ignore").lower()
            for name in names:
                assert name.lower() not in text, (f, name)
    assert usage_build.DB not in (importers.MAIN_DB, importers.SERVED_DB, importers.DB)
    # the main build runs the main project only
    from coffeedata import cli

    build_src = inspect.getsource(cli.dbt_build)
    assert set(re.findall(r'"--project-dir",\s*"([^"]+)"', build_src)) == {"dbt"} and "usage" not in build_src
    # only cmd_usage_build and cmd_geo_update in the CLI name the usage build
    tree = ast.parse((REPO / "src/coffeedata/cli.py").read_text())
    users = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Name) and node.id == "usage_build") or (
                    isinstance(node, ast.Constant) and "usage.duckdb" in str(node.value)
                ):
                    users.add(fn.name)
    assert users == {"cmd_usage_build"}, users


def test_the_usage_project_builds_only_into_its_own_database():
    main = yaml.safe_load((REPO / "dbt/dbt_project.yml").read_text())
    private = yaml.safe_load((REPO / "dbt_usage/dbt_project.yml").read_text())
    assert main["profile"] != private["profile"] and main["name"] != private["name"]
    profile = yaml.safe_load((REPO / "dbt_usage/profiles.yml").read_text())
    outputs = profile[private["profile"]]["outputs"]
    assert [o["path"] for o in outputs.values()] == ["{{ env_var('COFFEEDATA_USAGE_DB') }}"]
    assert private["on-run-start"] == ["{{ refuse_unless_private_target() }}"]
    assert f"set fixed = '{usage_build.DB}'" in (REPO / "dbt_usage/macros/private_target.sql").read_text()
    # no failing row of any test is ever stored (the detector's would be search text)
    assert private["data_tests"]["coffeedata_usage"]["+store_failures"] is False
    assert "store_failures=false" in (REPO / "dbt_usage/tests/search_text_looks_personal.sql").read_text()


def test_the_private_build_refuses_a_public_database_or_folder(checkout, monkeypatch):
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("COFFEEDATA_DB", usage_build.DB)  # the MCP server would read it
    with pytest.raises(ValueError, match="public build's database"):
        usage_build.private_db()
    monkeypatch.delenv("COFFEEDATA_DB")
    monkeypatch.setenv("PUBLIC_EXPORT_URL", "build")
    with pytest.raises(ValueError, match="inside a folder a public job uploads"):
        usage_build.private_db()


def test_only_the_location_model_reads_the_ip_prefix():
    """No model's SQL but the staging view over raw and int_usage_location names the prefix (design section 4); the
    dbt test ip_prefix_only_in_location checks the built columns."""
    readers = set()
    for f in (usage_build.PROJECT / "models").rglob("*.sql"):
        if re.search(r"\bip_prefix\b", f.read_text()):
            readers.add(f.stem)
    assert readers == {"stg_usage_ip_prefixes", "int_usage_location"}
    for f in (usage_build.PROJECT / "macros").rglob("*.sql"):
        text = f.read_text()
        if re.search(r"\bip_prefix\b", text):
            assert f.name in ("usage_raw.sql", "usage_events.sql"), f
    location = (usage_build.PROJECT / "models/intermediate/int_usage_location.sql").read_text()
    final = location.rsplit("select e.event_id", 1)[1].split("from", 1)[0]
    assert "ip_prefix" not in final and "start_hex" not in final  # the model's output never carries it


def test_no_model_reads_the_wall_clock_or_randomness():
    banned = re.compile(r"\b(now\(\)|current_timestamp|current_date|today\(\)|run_started_at|random\(\)|uuid\(\)|"
                        r"using sample)", re.I)  # fmt: skip
    for f in [
        *(usage_build.PROJECT / "models").rglob("*.sql"),
        *(usage_build.PROJECT / "macros").rglob("*.sql"),
    ]:
        assert not banned.search(f.read_text()), f


def test_every_usage_model_has_a_description_and_a_grain_test():
    schema = yaml.safe_load((usage_build.PROJECT / "models/schema.yml").read_text())
    described = {m["name"]: m for m in schema["models"]}
    assert set(described) == usage_build.models()
    for name, m in described.items():
        assert m.get("description", "").startswith("One row per"), name
        tests = json.dumps(m)
        assert '"grain"' in tests or '"unique"' in tests, name


def test_the_public_facts_only_guard_names_the_usage_columns():
    """public_tables_are_facts_only (dbt/tests/) refuses every usage column in a public table; tests/test_dbt.py makes
    it fail on a bad column. city and state are allowed in public_roaster only (the roaster's location), event in
    public_price_event only."""
    text = (REPO / "dbt/tests/public_tables_are_facts_only.sql").read_text()
    parts = re.findall(r"'([^']*)'", text.split("regexp_matches(column_name,", 1)[1].split("and not", 1)[0])
    pattern = "".join(parts)
    assert pattern.startswith("^(") and pattern.endswith(")$")
    for col in ("query", "ip", "ip_prefix", "source", "referrer_domain", "utm_source", "utm_medium", "utm_campaign",
                "rank", "surface", "device", "region", "country", "bot", "tags", "city", "state", "event"):  # fmt: skip
        assert re.fullmatch(pattern, col), col
    for col in ("roaster_state", "origin_country", "attributes_source", "source_url"):
        assert not re.fullmatch(pattern, col), col
    assert "table_name = 'public_roaster' and column_name in ('city', 'state')" in text
    assert "table_name = 'public_price_event' and column_name = 'event'" in text


def test_the_facts_only_guard_allows_city_only_in_public_roaster(tmp_path):
    """The guard's SQL on a database holding public tables with the allowed columns and with visitor ones."""
    sql = (REPO / "dbt/tests/public_tables_are_facts_only.sql").read_text()
    sql = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    with duckdb.connect(str(tmp_path / "x.duckdb")) as con:
        con.execute("create table public_roaster (roaster_id varchar, city varchar, state varchar)")
        con.execute("create table public_price_event (event varchar)")
        con.execute("create table public_coffee (coffee_id varchar, roaster_state varchar)")
        assert con.execute(sql).fetchall() == []
        con.execute("alter table public_coffee add column city varchar")
        con.execute("alter table public_coffee add column event varchar")
        con.execute("alter table public_price_event add column state varchar")
        assert sorted(con.execute(sql).fetchall()) == [
            ("public_coffee", "city"), ("public_coffee", "event"), ("public_price_event", "state")]  # fmt: skip


def test_the_coded_values_are_the_collectors():
    """dbt's accepted values are the collector's (lake.USAGE_VALUES, contract usage 1.0.0), once that is merged."""
    from coffeedata import lake

    values = getattr(lake, "USAGE_VALUES", None)
    if values is None:
        pytest.skip("the collector's lake.USAGE_VALUES is not on this branch yet")
    project = yaml.safe_load((usage_build.PROJECT / "dbt_project.yml").read_text())["vars"]
    for key, var in (("event", "usage_events"), ("surface", "usage_surfaces"), ("device", "usage_devices"),
                     ("sort", "usage_sorts"), ("source", "usage_sources_kinds")):  # fmt: skip
        assert tuple(project[var]) == tuple(values[key]), key
    assert lake.GLOBS["usage_events"] == usage_build.EVENTS_GLOB


def test_geo_update_counts_the_events_whose_city_would_change(tmp_path):
    data = tmp_path / "usage"
    write_dataset(data, synthetic_events())
    write_geo(data, tmp_path / "old.lock")
    old = tmp_path / "old.parquet"
    shutil.copy(data / "geo/dbip-test.parquet", old)
    moved = [r if r[4] != "Denver" else (*r[:4], "Lakewood") for r in GEO_RANGES]
    write_geo(data, tmp_path / "new.lock", ranges=moved)
    # every raw row counts: the retried batch's copies of the first three (Denver) events and the early copy too
    denver = sum(1 for e in synthetic_events() if e["ip_prefix"] == "198.51.100.0/24") + 4
    assert geo.changed_events(usage_build.sources(str(data)), old, data / "geo/dbip-test.parquet") == denver
    assert geo.changed_events([], old, old) == 0


@needs_dbt
def test_the_dbt_unit_tests_pass(checkout):
    """dbt_usage/models/unit_tests.yml: the location lookup, the source and query rules, the city floors and the
    query thresholds over hand-written rows."""
    data = checkout / "usage"
    write_dataset(data, synthetic_events())
    write_geo(data, checkout / "geo.lock")
    target = checkout / "target"
    usage_build.build(str(data), lock_path=checkout / "geo.lock", target=target, log=checkout / "dbt.log",
                      unit_tests=True)  # fmt: skip
    results = json.loads((target / "run_results.tests.json").read_text())["results"]
    units = {
        r["unique_id"].split(".")[-1]: r["status"] for r in results if r["unique_id"].startswith("unit_test.")
    }
    assert len(units) == 10 and set(units.values()) == {"pass"}, units


def test_consensus_takes_the_level_every_row_agrees_on():
    """geo.py's measurement and changed_events follow int_usage_location's rule (the dbt unit test pins the SQL)."""
    h = lambda ip: int(geo.to_hex(ip), 16)  # noqa: E731
    rows = sorted([
        (h("198.51.100.0"), h("198.51.100.127"), "US", "Colorado", "Denver (Five Points)"),
        (h("198.51.100.128"), h("198.51.100.255"), "US", "Colorado", "Denver"),
        (h("192.0.2.0"), h("192.0.2.127"), "US", "Colorado", "Boulder"),
        (h("192.0.2.128"), h("192.0.2.255"), "US", "Colorado", "Aurora"),
        (h("203.0.113.0"), h("203.0.113.127"), "US", "Washington", "Blaine"),
        (h("203.0.113.128"), h("203.0.113.255"), "CA", "British Columbia", "Surrey"),
        (h("10.0.0.0"), h("10.0.1.255"), "US", "Ohio", "Far"),
    ])  # fmt: skip
    v4 = geo.consensus_levels(rows)["ipv4_24"]
    assert v4["shared_blocks"] == 3 and v4["all_blocks"] == 5
    assert v4["shared"] == {"city": 0.3333, "region": 0.3333, "country": 0.0, "unknown": 0.3333}
    assert v4["all"]["city"] == 0.6  # the two whole /24s of 10.0.0.0/23 are Far
    assert geo.block("198.51.100.0/24") == (geo.to_hex("198.51.100.0"), geo.to_hex("198.51.100.255"))
    assert geo.block("2001:db8::/40")[1] == "20010db800" + "f" * 22
    assert geo.block("not an address") is None
    assert geo.placed({("US", "Colorado", "Denver")}) == ("US", "Colorado", "Denver")
    assert geo.placed({("US", "Colorado", "Boulder"), ("US", "Colorado", "Aurora")}) == (
        "US",
        "Colorado",
        None,
    )
    assert geo.placed({("US", "Oregon", "Portland"), ("US", "Washington", "Seattle")}) == ("US", None, None)
    assert geo.placed({("US", "Washington", "Blaine"), ("CA", "British Columbia", "Surrey")}) is None
    assert geo.base_city("Chicago (Near South Side)") == "Chicago" and geo.base_city(None) is None


def test_the_cli_prints_counts_only_and_alerts_when_the_detector_could_not_run(tmp_path, monkeypatch, capsys):
    """usage-build after a failed model step: no database, counts from both steps' results, and an alert that says the
    detector didn't run rather than none at all."""
    from coffeedata import cli

    monkeypatch.setattr(usage_build, "ROOT", tmp_path.resolve())

    def failed(**kw):
        target = kw["target"]
        (target / "run_results.models.json").write_text(json.dumps({"results": [
            {"unique_id": "model.x.a", "status": "success", "message": "SECRET-VALUE"},
            {"unique_id": "model.x.b", "status": "error", "message": "Conversion Error: 'canary@example.com'"}]}))  # fmt: skip
        return False

    monkeypatch.setattr(usage_build, "build", failed)
    alerts = tmp_path / "alerts.json"
    with pytest.raises(SystemExit):
        cli.main(["usage-build", "--alerts-out", str(alerts)])
    out = capsys.readouterr()
    assert '"failed"' in out.out and '"error": 1' in out.out and '"success": 1' in out.out
    assert "canary" not in out.out + out.err and "SECRET" not in out.out + out.err
    (alert,) = json.loads(alerts.read_text())
    assert alert["firing"] and alert["checks"] == [{"name": "detector ran", "status": "warning"}]


@needs_dbt
def test_a_failed_model_leaves_no_database_and_dbt_never_prints(checkout, capfd):
    """A model that fails (a raw value that won't cast) deletes the half-built database; dbt's output, which can quote
    the value, goes to build/usage-dbt.log, never the console."""
    data = checkout / "usage"
    write_geo(data, checkout / "geo.lock")
    folder = data / "events" / "day=2026-09-07"
    folder.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([{"event_id": "x", "received_at": "not-a-time-canary", "event": "search"}]),
                   folder / "batch=a.parquet")  # fmt: skip
    ok = usage_build.build(str(data), lock_path=checkout / "geo.lock", target=checkout / "target")
    assert not ok and not (checkout / usage_build.DB).exists()
    out = capfd.readouterr()
    assert "not-a-time-canary" not in out.out + out.err
    assert (checkout / usage_build.DBT_LOG).exists()
    assert not (
        checkout / "target" / "run_results.tests.json"
    ).exists()  # the tests never ran on a partial build
