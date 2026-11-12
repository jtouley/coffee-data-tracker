"""The catalog's search and filters in a real browser (tests/browser/catalog_flow.js), at 390 px and on desktop, light
and dark, and at 320 px with the charts: the roaster's city and state in the search box (cities and state names,
two-letter codes as whole words only), the State filter, "Show more" past the first 500 rows, a slow catalog.json and a
failed one with Try again. The page is the real template, rendered as the site renders it; its catalog.json is a small one written
here. Needs Node with Playwright (skipped without it, except under REQUIRE_BROWSER, as in CI); axe runs where
axe-core is installed. CATALOG_SCREENSHOTS=<dir> keeps the screenshots."""

import json
import os
import subprocess
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from jinja2 import Environment, PackageLoader, select_autoescape

from coffeedata import site

from .test_app_browser import BROWSER, _playwright
from .test_smoke_web import QuietFiles

pytestmark = pytest.mark.skipif(
    not _playwright(axe=False) and not os.environ.get("REQUIRE_BROWSER"),
    reason="Node with Playwright not installed",
)

# (name, city, state): Miami holds "mi", Michigan City sits in Indiana, Oakland's "La Paloma" holds "la", two roasters
# are in the two Kansas Citys, York, PA sits inside "New York", and one roaster has no city.
ROASTERS = [
    ("capitolbeans.com", "Capitol Beans", "Washington", "DC"),
    ("drycreek.coffee", "Dry Creek Roasting", None, "TX"),
    ("empire.coffee", "Empire Roasters", "Brooklyn", "NY"),
    ("gotham.coffee", "Gotham Roasters", "New York", "NY"),  # "new york city" and "nyc" are this city
    ("lapaloma.coffee", "La Paloma Coffee", "Oakland", "CA"),
    ("littlehavana.coffee", "Little Havana Roasters", "Miami", "FL"),
    ("maplecity.coffee", "Maple City Coffee", "Michigan City", "IN"),
    ("mitten.coffee", "Mitten Coffee Co", "Detroit", "MI"),
    ("northside.coffee", "Northside Coffee", "Chicago", "IL"),
    ("ozark.coffee", "Ozark Beans", "Fayetteville", "AR"),
    ("plaza.coffee", "Plaza Roasters", "Kansas City", "MO"),
    ("hill.coffee", "Strawberry Hill Roasters", "Kansas City", "KS"),
    ("sumatrahouse.coffee", "Sumatra House", "Portland", "OR"),
    ("yorkbeans.coffee", "York Beans", "York", "PA"),
    ("littleseed.coffee", "little seed coffee", "Boise", "ID"),  # "new york" is the state, not York, PA
]
# main's columns, then the two added at the end
COLUMNS = ["coffee_id", "roaster_name", "roaster_state", "title", "url", "origin_country", "process", "roast_level",
           "tasting_notes", "price_per_100g", "min_price_usd", "in_stock", "catalog_status", "first_seen",
           "roaster_id", "roaster_city"]  # fmt: skip
BUILD = "20261110T061836-test01"
KEY = "0123abcd4567"  # site.build passes a hash of catalog.json


def coffee(
    roaster: str, title: str, process: str = "washed", notes=("cocoa",), in_stock: bool = True, price=6.5
) -> dict:
    rid, name, city, state = next(r for r in ROASTERS if r[0] == roaster)
    return {"coffee_id": f"{rid}:{title}", "roaster_name": name, "roaster_state": state, "title": title,
            "url": f"https://{rid}/", "origin_country": "Ethiopia", "process": process, "roast_level": "light",
            "tasting_notes": list(notes), "price_per_100g": price, "min_price_usd": 22.0, "in_stock": in_stock,
            "catalog_status": "active", "first_seen": "2026-10-26", "roaster_id": rid, "roaster_city": city}  # fmt: skip


def catalog() -> dict:
    """620 coffees in stock from the Michigan roaster (past the 500-row page) and one or two from each other, in the
    site's order (roaster, then title). One Michigan coffee is a "City Roast", which "michigan city coffee" mustn't
    find."""
    rows = [
        coffee("capitolbeans.com", "Capitol Roast", notes=("zebra",)),
        coffee("drycreek.coffee", "Dry Creek Blend", price=None),
        coffee("empire.coffee", "Empire Blend", notes=("vermont maple",), price=9.0),
        coffee("gotham.coffee", "Gotham Blend", price=None),
        coffee("lapaloma.coffee", "Paloma House"),
        coffee("littlehavana.coffee", "Miami Nights", notes=("mild", "cacao")),
        coffee("littlehavana.coffee", "Michigan Cherry"),
        coffee("maplecity.coffee", "Lakefront Blend", notes=("nutty",)),
    ]
    rows += [
        coffee("mitten.coffee", f"Mitten Cherry {i:03}" if i <= 20 else "Mitten City Roast 021" if i == 21 else f"Mitten {i:03}",
               process="natural" if i <= 10 else "washed", notes=("mild",))
        for i in range(1, 621)
    ]  # fmt: skip
    rows += [
        coffee("mitten.coffee", "Mitten Old Crop", in_stock=False),
        coffee("northside.coffee", "Windy City Espresso"),
        coffee("ozark.coffee", "Ozark Morning"),
        coffee("plaza.coffee", "Plaza Espresso"),
        coffee("hill.coffee", "Hill Blend", price=4.0),
        coffee("sumatrahouse.coffee", "Indonesia Sumatra", notes=("dried fig", "made in small batches")),
        coffee("yorkbeans.coffee", "Brand New Roast", notes=("zebra",)),
        # A lower-case name, last in the file as a binary sort would put it; the page puts it with the L's.
        coffee("littleseed.coffee", "Seed Roast", notes=("zebra",)),
    ]
    return {
        "release": {"release_build_id": BUILD},
        "columns": COLUMNS,
        "rows": [[r[c] for c in COLUMNS] for r in rows],
    }


def render(out: Path) -> None:
    """The catalog page as site.build renders it, with its two charts drawn by site.hbar_chart (they set the page's
    width at 320 px) and empty counts."""
    origins = [("Papua New Guinea", 4.51), ("Ethiopia", 5.07), ("Colombia", 4.71), ("Kenya", 5.51)]
    processes = [("not stated", 11033), ("washed", 2389), ("natural", 1423), ("anaerobic", 284)]
    charts = {
        "origin_price": site.hbar_chart(
            [(o, v, f"{o}: ${v:.2f}") for o, v in origins], "${:.2f}", "Price by origin"
        ),
        "process": site.hbar_chart([(p, n, f"{p}: {n:,}") for p, n in processes], "{:,.0f}", "By process"),
    }
    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    html = env.get_template("index.html").render(
        links=site.links(), jsonld="", totals={"coffees": 0, "in_stock": 0, "roasters": 0}, status_rows=[],
        charts=charts, new=[], coverage={"registry": {}}, origin_price=[{"origin_country": o, "n": 5, "med": v} for o, v in origins],
        processes=[{"process": p, "n": n} for p, n in processes], generated="2026-11-09 12:00 UTC", data_key=KEY, state_titles=site.STATE_TITLES,
        state_words=site.STATE_WORDS,
    )  # fmt: skip
    (out / "catalog").mkdir(parents=True)
    (out / "catalog" / "index.html").write_text(site.with_csp(html))
    (out / "catalog" / "catalog.json").write_text(json.dumps(catalog()))


def test_catalog_search_and_state_filter_in_a_browser(tmp_path):
    render(tmp_path / "site")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietFiles, directory=str(tmp_path / "site")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shots = Path(os.environ.get("CATALOG_SCREENSHOTS") or tmp_path / "shots")
    shots.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["node", str(BROWSER / "catalog_flow.js"), f"http://127.0.0.1:{server.server_port}", str(shots), KEY],
            capture_output=True, text=True, timeout=300, cwd=BROWSER,
        )  # fmt: skip
    finally:
        server.shutdown()
        thread.join()
    assert r.returncode == 0, r.stderr[-3000:]
    assert {p.name for p in shots.iterdir()} == {
        f"catalog-michigan-{w}-{scheme}.png"
        for w in ("320", "390", "desktop", "zoom200")
        for scheme in ("light", "dark")
    }


def test_state_words_read_the_longest_name_first():
    """ "west virginia" is read as one state before "virginia", and every code has a name to show."""
    names = [name for name, _ in site.STATE_WORDS]
    assert names.index("west virginia") < names.index("virginia")
    assert names.index("district of columbia") < names.index("washington")
    assert site.STATE_TITLES["DC"] == "District of Columbia" and site.STATE_TITLES["NC"] == "North Carolina"
    assert set(site.STATE_TITLES) == set(site.US_STATES)
