"""Build the dbt project over a synthetic lake and check the derived history (the same SQL the weekly run uses)."""

import json
import os
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from coffeedata import registry

from .lakehelp import probe, product, run

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")


def build(lake, tmp_path, **extra_vars):
    db = tmp_path / "coffee.duckdb"
    if db.exists():
        db.unlink()
    r = subprocess.run(
        [
            "dbt",
            "build",
            "--project-dir",
            "dbt",
            "--profiles-dir",
            "dbt",
            "--vars",
            json.dumps({"lake_roots": [str(lake.root)], "config_dir": str(FIX)} | extra_vars),
            "--target-path",
            str(tmp_path / "target"),
            "--log-path",
            str(tmp_path / "logs"),
        ],
        cwd=ROOT,
        env=os.environ | {"COFFEEDATA_DB": str(db)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout[-4000:]
    return duckdb.connect(str(db), read_only=True)


def test_history_is_derived_from_successful_runs(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    run(
        lake,
        "2026-10-19",
        rid,
        [product("A", 20.0), product("B", 18.0), product("M", coffee=False, title="Mug")],
    )
    run(lake, "2026-10-26", rid, None)  # failed fetch: must not look like a delisting
    run(lake, "2026-11-02", rid, [product("A", 22.0), product("C", 25.0)])
    con = build(lake, tmp_path)

    events = sorted(
        con.execute(
            "select run_date, coffee_id, event, price_usd, previous_price_usd from fct_price_event"
        ).fetchall()
    )
    d1, d3 = date(2026, 10, 19), date(2026, 11, 2)
    assert events == [
        (d1, f"{rid}:A", "new", 20.0, None),
        (d1, f"{rid}:B", "new", 18.0, None),
        (d3, f"{rid}:A", "price", 22.0, 20.0),
        (d3, f"{rid}:B", "removed", None, 18.0),
        (d3, f"{rid}:C", "new", 25.0, None),
    ]
    coffees = {
        r[0]: r[1:]
        for r in con.execute(
            "select product_id, is_listed, in_stock, first_seen, last_seen from dim_coffee"
        ).fetchall()
    }
    assert coffees["B"] == (False, False, d1, d1)  # delisted but kept: history still resolves (finding 6)
    assert coffees["A"][:2] == (True, True) and "M" not in coffees
    assert con.execute("select status from dim_roaster where roaster_id = ?", [rid]).fetchone()[0] == "active"
    assert con.execute("select count(*) from mart_new_arrivals").fetchone()[0] == 1  # C

    # Re-running a week (another successful batch the same day) changes nothing downstream.
    run(lake, "2026-11-02", rid, [product("A", 22.0), product("C", 25.0)], hour=18)
    con2 = build(lake, tmp_path)
    assert (
        sorted(
            con2.execute(
                "select run_date, coffee_id, event, price_usd, previous_price_usd from fct_price_event"
            ).fetchall()
        )
        == events
    )


def test_new_snapshot_column_keeps_raw_immutable_and_builds_idempotent(lake, tmp_path, monkeypatch):
    import hashlib

    from coffeedata import lake as lake_mod

    rid = "onyxcoffeelab.com"
    # Week 1 written before snapshots had published_at; week 2 after.
    old = lake_mod.SCHEMAS["snapshots"]
    monkeypatch.setitem(lake_mod.SCHEMAS, "snapshots", old.remove(old.get_field_index("published_at")))
    run(lake, "2026-10-19", rid, [product("A"), product("B", available=False) | {"tags": ["Archived"]}])
    monkeypatch.setitem(lake_mod.SCHEMAS, "snapshots", old)
    pub = datetime(2024, 3, 1, 14, 30, tzinfo=UTC)
    run(lake, "2026-10-26", rid, [product("A") | {"published_at": pub}, product("C") | {"published_at": pub}])

    def raw() -> dict[str, str]:  # every data file in the lake, by content (schema files are zero-row)
        return {str(p.relative_to(lake.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in lake.root.rglob("*.parquet") if "schema" not in p.name}  # fmt: skip

    def marts(con, observed: bool = True) -> dict[str, list]:
        cols = {m: "*" for m in ("dim_coffee", "public_coffee")}
        cols["fct_price_event"] = "*" if observed else "* exclude (fetched_at)"
        return {m: con.execute(f"select {c} from {m} order by all").fetchall() for m, c in cols.items()}

    before = raw()
    con = build(lake, tmp_path)
    first, first_unobserved = marts(con), marts(con, observed=False)
    lake_mod.Lake()  # refreshes the local schema file only
    second = marts(build(lake, tmp_path))
    assert raw() == before and first == second  # raw untouched; same lake, same tables

    got = {r[0]: r[1:] for r in build(lake, tmp_path).execute(
        "select product_id, catalog_status, published_at from dim_coffee").fetchall()}  # fmt: skip
    assert got == {"A": ("available", pub), "B": ("delisted", None), "C": ("available", pub)}

    # Re-running the week adds a batch (never rewrites one) and changes nothing downstream.
    run(
        lake,
        "2026-10-26",
        rid,
        [product("A") | {"published_at": pub}, product("C") | {"published_at": pub}],
        hour=18,
    )
    assert set(before) < set(raw()) and all(raw()[k] == v for k, v in before.items())
    # The latest successful batch of a week wins (a forced re-fetch replaces bad data), so only the time its
    # events were observed moves; every coffee, status, price and event is the same.
    assert marts(build(lake, tmp_path), observed=False) == first_unobserved


def test_catalog_status_separates_current_from_back_catalog(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    tagged = lambda pid, tags, **kw: product(pid, **kw) | {"tags": tags}  # noqa: E731
    run(lake, "2026-10-19", rid, [product("GONE")])
    run(
        lake,
        "2026-10-26",
        rid,
        [
            product("NOW"),
            product("OUT", available=False),
            tagged("OLD", ["Archived"], available=False),
            tagged("OLD2", ["past-offering"], available=False),
            tagged("SOON", ["preorder-ship-date:2026-11-05"]),
            tagged(
                "BACK", ["archive"]
            ),  # archived, but buyable again: what matters now is that it's in stock
            tagged("PASTA", ["Pasta"], available=False),
            tagged("SLUG", ["past_offering"], available=False),
            tagged("SOON2", ["coming-soon"]),
        ],
    )
    con = build(lake, tmp_path)
    got = dict(con.execute("select product_id, catalog_status from dim_coffee").fetchall())
    assert got == {
        "GONE": "delisted",
        "NOW": "available",
        "OUT": "sold_out",
        "OLD": "archived",
        "OLD2": "archived",
        "SOON": "preorder",
        "BACK": "available",
        "PASTA": "sold_out",
        "SLUG": "archived",
        "SOON2": "preorder",
    }


def test_relisting(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    run(lake, "2026-10-19", rid, [product("A"), product("B")])
    run(lake, "2026-10-26", rid, [product("A")])
    run(lake, "2026-11-02", rid, [product("A"), product("B")])
    con = build(lake, tmp_path)
    b = con.execute(
        "select run_date, event from fct_price_event where coffee_id like '%:B' order by 1"
    ).fetchall()
    assert b == [
        (date(2026, 10, 19), "new"),
        (date(2026, 10, 26), "removed"),
        (date(2026, 11, 2), "relisted"),
    ]


def test_repeated_variant_ids_in_raw_snapshots(lake, tmp_path):
    # Raw snapshots written before the generic adapter de-duplicated SKUs: same variant_id on every offer.
    p = product("A")
    p["variants"] = [
        p["variants"][0] | {"variant_id": "SKU", "title": t} for t in ("Whole bean", "Drip", "Espresso")
    ]
    run(lake, "2026-10-26", "ikoncoffee.com", [p])
    con = build(lake, tmp_path)  # unique_price_events would fail without the staging fix
    assert sorted(con.execute("select variant_id, event from fct_price_event").fetchall()) == [
        ("SKU", "new"),
        ("SKU#1", "new"),
        ("SKU#2", "new"),
    ]


def test_python_and_dbt_agree_on_roaster_status(lake, tmp_path):
    a, b, c = "onyxcoffeelab.com", "intelligentsia.com", "vividcoffee.com"
    run(lake, "2026-10-19", a, [product("1")])
    run(lake, "2026-10-19", b, [product("1", coffee=False)])
    for d in ("2026-10-19", "2026-10-26", "2026-11-02"):
        run(lake, d, c, None)
    probe(lake, "2026-11-03", a, status="unreachable")
    con = build(lake, tmp_path)
    dbt_status = dict(con.execute("select roaster_id, status from dim_roaster").fetchall())
    py_status = {s.roaster.roaster_id: s.status for s in registry.current(lake, FIX / "roasters.csv")}
    assert dbt_status == py_status
    assert (py_status[a], py_status[b], py_status[c]) == ("active", "no_coffee", "unreachable")


def test_public_tables_are_facts_only_and_filtered(lake, tmp_path):
    from datetime import UTC, datetime

    from coffeedata import public

    now = datetime(2026, 10, 1, tzinfo=UTC)
    lake.write(
        "registry",
        "registry/source=osm/x.parquet",
        [{"added_at": now, "source": "osm", "roaster_id": "osmonly.coffee", "name": "OSM Only"}],
    )
    lake.write(
        "registry",
        "registry/source=manual/y.parquet",
        [{"added_at": now, "source": "manual", "roaster_id": "vividcoffee.com", "opted_out": True}],
    )
    for rid in ("onyxcoffeelab.com", "osmonly.coffee", "vividcoffee.com"):
        probe(lake, "2026-10-18", rid)
        run(lake, "2026-10-19", rid, [product("A"), product("M", coffee=False)])
    con = build(lake, tmp_path)

    roasters = {r[0] for r in con.execute("select roaster_id from public_roaster").fetchall()}
    assert "onyxcoffeelab.com" in roasters
    assert "osmonly.coffee" not in roasters and "vividcoffee.com" not in roasters  # ODbL-only, opted out
    assert {r[0] for r in con.execute("select distinct roaster_id from public_coffee").fetchall()} == {
        "onyxcoffeelab.com"
    }
    cols = {
        r[0]
        for r in con.execute(
            "select column_name from information_schema.columns where table_name = 'public_coffee'"
        ).fetchall()
    }
    assert "source_url" in cols and not cols & {"image", "handle", "product_id", "body_hash", "tags"}
    con.close()

    out = public.export(tmp_path / "coffee.duckdb", str(tmp_path / "public"))
    assert out["tables"]["public_coffee"] == 1
    assert sorted(p.name for p in (tmp_path / "public").iterdir()) == [
        "manifest.json",
        "public_coffee.parquet",
        "public_market_weekly.parquet",
        "public_price_event.parquet",
        "public_roaster.parquet",
    ]


def test_old_and_new_shots_with_equipment_and_bags(lake, tmp_path):
    from datetime import datetime

    from coffeedata.lake import SCHEMAS
    from coffeedata.models import Bag, Equipment, Shot
    from coffeedata.shots import record

    from .lakehelp import at, old_shot

    rid = "onyxcoffeelab.com"
    run(lake, "2026-10-19", rid, [product("A", title="Geometry")])
    old_shot(lake, "old1", "2026-10-20", coffee_id=f"{rid}:A", rating=4)
    old_shot(lake, "old2", "2026-10-21", coffee_freetext="Cafe bag", time_s=22.0)

    def equip(e: Equipment, when: datetime) -> None:
        lake.write(
            "equipment",
            f"equipment/{e.equipment_id}__{when:%Y%m%dT%H%M%S}.parquet",
            [e.model_dump() | {"logged_at": when}],
        )

    g = Equipment(
        equipment_id="g1",
        user_id="u1",
        kind="grinder",
        name="Grinder",
        scale_min=0,
        scale_max=90,
        scale_step=0.5,
    )
    equip(g, at("2026-10-22", 6))
    equip(g.model_copy(update={"name": "DF64 V2"}), at("2026-10-22", 7))  # a correction: latest wins
    bag = Bag(
        bag_id="b1",
        user_id="u1",
        coffee_id=f"{rid}:A",
        coffee_freetext="Onyx Geometry",
        roast_date="2026-10-15",
    )
    lake.write(
        "bags", "bags/b1__20261022T060000.parquet", [bag.model_dump() | {"logged_at": at("2026-10-22", 6)}]
    )
    for i, (grind, rating) in enumerate([(11.0, 3), (10.0, 5)]):
        record(
            Shot(
                shot_id=f"new{i}",
                brewed_at=at("2026-10-23", 7 + i),
                grinder_setting=grind,
                dose_g=18,
                yield_g=36,
                time_s=29,
                rating=rating,
                user_id="u1",
                grinder_id="g1",
                bag_id="b1",
                source="app",
            ),
            lake,
        )
    assert set(SCHEMAS["shots"].names) - {"logged_at", "deleted"} <= set(Shot.model_fields) | {"logged_at"}

    con = build(lake, tmp_path)
    rows = {
        r[0]: r[1:]
        for r in con.execute(
            "select shot_id, user_id, coffee_id, roast_date, grinder_name, grind_position, source from fct_shot"
        ).fetchall()
    }
    assert rows["old1"] == ("owner", f"{rid}:A", None, None, None, None)  # unchanged; the owner's
    assert rows["old2"][:2] == ("owner", None)
    # The bag supplies the roast date; the grinder its name and scale.
    assert rows["new1"] == ("u1", f"{rid}:A", date(2026, 10, 15), "DF64 V2", round(10 / 90, 3), "app")
    dial = con.execute(
        "select user_id, grinder_name, shots, best_grind from mart_dial_in where grain = 'coffee' order by 1, 2"
    ).fetchall()
    assert dial == [("owner", None, 1, 12.0), ("owner", None, 1, 12.0), ("u1", "DF64 V2", 2, 10.0)]
    assert con.execute("select count(*) from dim_equipment").fetchone()[0] == 1
    # One row per bag: its shots, the best one, and days off roast at the first and last shot.
    assert con.execute(
        "select user_id, bag_id, coffee_id, shot_count, best_shot_id, best_rating, days_off_roast_first,"
        " days_off_roast_last, shots_in_window from mart_bag"
    ).fetchall() == [("u1", "b1", f"{rid}:A", 2, "new1", 5, 8, 8, 2)]


def test_only_old_shot_files_still_build(lake, tmp_path):
    """A lake whose shots all predate the app's columns: the v2 schema file supplies them as null."""
    from .lakehelp import old_shot

    old_shot(lake, "old1", "2026-10-20", coffee_freetext="x")
    con = build(lake, tmp_path)
    assert con.execute("select shot_id, user_id, grinder_id, bag_id from fct_shot").fetchall() == [
        ("old1", "owner", None, None)
    ]


def test_site_has_no_shots(lake, tmp_path):
    from coffeedata import site

    from .lakehelp import old_shot

    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A")])
    old_shot(lake, "secretshot", "2026-10-20", coffee_freetext="Private Blend", notes="kitchen notes")
    build(lake, tmp_path).close()
    out = site.build(tmp_path / "coffee.duckdb", tmp_path / "site")
    # Every file the site writes (landing, catalog, legal pages): none carries a shot's data. (The landing page
    # does mention that shots can be logged through the MCP server.)
    text = "".join(p.read_text() for p in out.parent.rglob("*") if p.is_file()).lower()
    assert not any(w in text for w in ("secretshot", "private blend", "kitchen notes", "rated "))
    assert "rating by brew ratio" not in text and "recent shots" not in text


def test_site_has_a_launch_page_and_the_catalog_app(lake, tmp_path, monkeypatch):
    from coffeedata import site

    monkeypatch.setenv("PUBLIC_EXPORT_URL", "hf://datasets/someone/us-coffee-market")
    probe(lake, "2026-10-11", "onyxcoffeelab.com")
    run(lake, "2026-10-12", "onyxcoffeelab.com", [product("A")])
    evil = product("E") | {"url": "javascript:alert(1)"}
    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A", price=22.0), product("B"), evil])
    con = build(lake, tmp_path)
    # The unsafe link is dropped in the model, so the public tables, exports and MCP server never see it.
    assert con.execute("select url from dim_coffee where title = 'Coffee E Roast'").fetchone() == (None,)
    assert con.execute(
        "select count(*) from public_coffee where coffee_name = 'Coffee E Roast'"
    ).fetchone() == (0,)
    con.close()

    out = tmp_path / "site"
    site.build(tmp_path / "coffee.duckdb", out)
    landing = (out / "index.html").read_text()
    assert 'href="catalog/"' in landing
    assert "https://huggingface.co/datasets/someone/us-coffee-market" in landing
    assert "hf://datasets/someone/us-coffee-market/data/public_coffee.parquet" in landing
    assert "Coffee B Roast" in landing  # a new arrival
    assert 'href="../"' in (out / "catalog" / "index.html").read_text()
    for page, heading in (("privacy", "Privacy Policy"), ("terms", "Terms of Use")):
        html = (out / page / "index.html").read_text()
        assert f'<h1 class="serif">{heading}</h1>' in html and f"Last updated {site.LEGAL_UPDATED}" in html
        assert "Content-Security-Policy" in html and "support@registry.coffee" in html
        assert f'href="{page}/"' in landing
        assert f'href="../{page}/"' in (out / "catalog" / "index.html").read_text()
    assert "State of Michigan" in (out / "terms" / "index.html").read_text()
    catalog = json.loads((out / "catalog" / "catalog.json").read_text())
    assert catalog["rows"]
    app = (out / "catalog" / "index.html").read_text()
    for html in (landing, app):
        assert "javascript:" not in html
        assert "Content-Security-Policy" in html and "script-src 'sha256-" in html
    assert None in [r[catalog["columns"].index("url")] for r in catalog["rows"]]  # the javascript: link


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://onyxcoffeelab.com/products/geometry", True),
        ("http://x.coffee/p?id=1", True),
        ("javascript:alert(1)", False),
        ("JAVASCRIPT:alert(1)", False),
        ("data:text/html,hi", False),
        ('https://x.com/"><script>', False),
        ("https://x.com/a\n", False),
        ("https://x.com/farmer's-reserve", True),
        (None, False),
    ],
)
def test_safe_url(url, ok):
    from coffeedata import site

    assert site.safe_url(url) == (url if ok else None)


def test_site_links_without_a_public_hf_dataset():
    from coffeedata import site

    assert site.links("s3://bucket/coffee", "https://example.org/")["dataset"] is None
    assert site.links("hf://datasets/a/b@main")["dataset"] == "https://huggingface.co/datasets/a/b"


def _landing(**reach) -> str:
    from jinja2 import Environment, PackageLoader, select_autoescape

    from coffeedata import site

    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    return env.get_template("landing.html").render(
        totals={"coffees": 1, "in_stock": 1, "roasters": 1},
        registry=1,
        reach={"origins": 1, "states": 1, "has_dc": False, "has_pr": False} | reach,
        week={"new": 0, "price_changes": 0, "removed": 0},
        new=[],
        charts={"origins": ""},
        links=site.links("", None),
        public_tables=4,
        generated="today",
    )


@pytest.mark.parametrize(
    "states, has_dc, has_pr, text",
    [
        (50, True, True, "across 50 states, DC and Puerto Rico."),
        (50, True, False, "across 50 states and DC."),
        (50, False, False, "across 50 states."),
        (1, False, False, "across 1 state."),
    ],
)
def test_landing_counts_dc_and_puerto_rico_apart_from_the_states(states, has_dc, has_pr, text):
    assert text in _landing(states=states, has_dc=has_dc, has_pr=has_pr)


def test_site_fallback_query_path_is_escaped_once():
    html = _landing()
    assert "hf://datasets/&lt;owner&gt;/&lt;name&gt;/data/public_coffee.parquet" in html
    assert "&amp;lt;" not in html


def test_csp_needs_somewhere_to_go():
    from coffeedata import site

    assert "Content-Security-Policy" in site.with_csp('<head><meta charset="utf-8"></head><script>x</script>')
    with pytest.raises(ValueError):
        site.with_csp("<head><meta charset=utf-8></head>")


@pytest.mark.parametrize(
    "url",
    ["hf://datasets/a/b", "hf://datasets/a/b@main", "hf://datasets/a/b/data", "hf://datasets/a/b/"],
)
def test_hf_repo_id(url):
    from coffeedata import public

    assert public.hf_repo_id(url) == "a/b"


def test_every_roaster_status_has_a_coverage_label():
    """A status added to dim_roaster without a label would silently drop out of the coverage table."""
    import re

    from coffeedata import site

    sql = (ROOT / "dbt" / "models" / "marts" / "dim_roaster.sql").read_text()
    statuses = set(re.findall(r"(?:then|else) '(\w+)'", sql))
    assert statuses and statuses == set(site.STATUS_LABELS)


def test_states_are_the_fifty():
    from coffeedata import site

    assert len(site.STATES) == 50 and "DC" not in site.STATES and "PR" not in site.STATES


def _rows(con, table):
    return con.execute(f"select * from {table} order by all").fetchall()


def test_the_same_lake_always_builds_the_same_tables(lake, tmp_path):
    """Idempotent builds: nothing in the marts depends on when or how often the build runs."""
    probe(lake, "2026-10-18", "onyxcoffeelab.com")
    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A"), product("B", 18.0)])
    run(lake, "2026-10-26", "onyxcoffeelab.com", [product("A", 22.0)])
    first = build(lake, tmp_path / "one")
    second = build(lake, tmp_path / "two")
    for table in ("dim_coffee", "fct_price_event", "mart_market", "public_coffee", "public_price_event"):
        assert _rows(first, table) == _rows(second, table), table


def test_classification_comes_from_the_current_rules_not_the_stored_flag(lake, tmp_path):
    """A rules fix reaches every past week: the flag and attributes stored at fetch time are ignored."""
    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    part = product(
        "P", title="La Marzocco (T.1.054) Tube, Valve To Discharge | Fits Mistral"
    )  # stored: coffee
    bean = product("K", coffee=False, title="Kenya Kamwangi AA Washed")  # stored: not coffee
    mocha = product("M", title="Mocha Java Blend")
    run(lake, "2026-10-19", rid, [part, bean, mocha])
    # An enrichment row from older rules said Mocha Java was from Indonesia.
    lake.write_partition(
        "enrichments",
        "2026-10-19",
        "old",
        [
            {
                "extracted_at": datetime(2026, 10, 19, 12, tzinfo=UTC),
                "roaster_id": rid,
                "product_id": "M",
                "body_hash": "bM",
                "method": "rules",
                "needs_llm": False,
                "origin_country": "Indonesia",
                "is_blend": False,
            }
        ],
    )
    con = build(lake, tmp_path)
    got = dict(con.execute("select product_id, origin_country from dim_coffee").fetchall())
    assert got == {"K": "Kenya", "M": None}
    events = {r[0] for r in con.execute("select coffee_id from fct_price_event").fetchall()}
    assert events == {f"{rid}:K", f"{rid}:M"}
    assert con.execute("select is_blend from dim_coffee where product_id = 'M'").fetchone() == (True,)


def test_claudes_extraction_still_overrides_the_rules(lake, tmp_path):
    rid = "onyxcoffeelab.com"
    probe(lake, "2026-10-18", rid)
    run(lake, "2026-10-19", rid, [product("A", title="House Espresso Roast")])
    lake.write_partition(
        "enrichments",
        "2026-10-19",
        "llm1",
        [
            {
                "extracted_at": datetime(2026, 10, 19, 12, tzinfo=UTC),
                "roaster_id": rid,
                "product_id": "A",
                "body_hash": "bA",
                "method": "llm",
                "needs_llm": False,
                "origin_country": "Brazil",
                "process": "natural",
                "is_blend": False,
            }
        ],
    )
    con = build(lake, tmp_path)
    assert con.execute("select origin_country, process, enrichment_method from dim_coffee").fetchone() == (
        "Brazil",
        "natural",
        "llm",
    )
