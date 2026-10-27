"""Build the dbt project over a synthetic lake and check the derived history (the same SQL the weekly run uses)."""

import json
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import duckdb
import pytest

from coffeedata import registry

from .lakehelp import probe, product, run

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed")


def build(lake, tmp_path):
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
            json.dumps({"lake_roots": [str(lake.root)], "config_dir": str(FIX)}),
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


def test_site_has_a_launch_page_and_the_catalog_app(lake, tmp_path, monkeypatch):
    from coffeedata import site

    monkeypatch.setenv("PUBLIC_EXPORT_URL", "hf://datasets/someone/us-coffee-market")
    probe(lake, "2026-10-11", "onyxcoffeelab.com")
    run(lake, "2026-10-12", "onyxcoffeelab.com", [product("A")])
    evil = product("E") | {"url": "javascript:alert(1)"}
    run(lake, "2026-10-19", "onyxcoffeelab.com", [product("A", price=22.0), product("B"), evil])
    con = build(lake, tmp_path)
    # The unsafe link is dropped in the model, so the public tables, exports and MCP server never see it.
    assert con.execute("select url from dim_coffee where title = 'Coffee E'").fetchone() == (None,)
    assert con.execute("select count(*) from public_coffee where coffee_name = 'Coffee E'").fetchone() == (0,)
    con.close()

    out = tmp_path / "site"
    site.build(tmp_path / "coffee.duckdb", out)
    landing = (out / "index.html").read_text()
    assert 'href="catalog/"' in landing
    assert "https://huggingface.co/datasets/someone/us-coffee-market" in landing
    assert "hf://datasets/someone/us-coffee-market/data/public_coffee.parquet" in landing
    assert "Coffee B" in landing  # a new arrival
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


def test_site_fallback_query_path_is_escaped_once():
    from jinja2 import Environment, PackageLoader, select_autoescape

    from coffeedata import site

    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    html = env.get_template("landing.html").render(
        totals={"coffees": 1, "in_stock": 1, "roasters": 1},
        registry=1,
        reach={"origins": 1, "states": 1},
        week={"new": 0, "price_changes": 0, "removed": 0},
        new=[],
        charts={"origins": ""},
        links=site.links("", None),
        public_tables=4,
        generated="today",
    )
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
