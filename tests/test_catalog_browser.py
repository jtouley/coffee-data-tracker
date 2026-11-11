"""The catalog's search and filters in a real browser (tests/browser/catalog_flow.js): the roaster's city and state in
the search box (state names, two-letter codes as whole words only), the State filter, and "Show more" past the first
500 rows. The page is the real template, rendered as the site renders it; its catalog.json is a small one written
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

# (name, city, state): Miami holds "mi", Michigan City sits in Indiana, Oakland's "La Paloma" holds "la", and one
# roaster has no city.
ROASTERS = [
    ("capitolbeans.com", "Capitol Beans", "Washington", "DC"),
    ("drycreek.coffee", "Dry Creek Roasting", None, "TX"),
    ("empire.coffee", "Empire Roasters", "Brooklyn", "NY"),
    ("lapaloma.coffee", "La Paloma Coffee", "Oakland", "CA"),
    ("littlehavana.coffee", "Little Havana Roasters", "Miami", "FL"),
    ("maplecity.coffee", "Maple City Roasters", "Michigan City", "IN"),
    ("mitten.coffee", "Mitten Coffee Co", "Detroit", "MI"),
    ("northside.coffee", "Northside Coffee", "Chicago", "IL"),
    ("ozark.coffee", "Ozark Beans", "Fayetteville", "AR"),
    ("sumatrahouse.coffee", "Sumatra House", "Portland", "OR"),
]
COLUMNS = ["coffee_id", "roaster", "title", "url", "origin_country", "process", "roast_level", "tasting_notes",
           "price_per_100g", "min_price_usd", "in_stock", "catalog_status", "first_seen"]  # fmt: skip


def coffee(
    roaster: str, title: str, process: str = "washed", notes=("cocoa",), in_stock: bool = True
) -> dict:
    i = [r[0] for r in ROASTERS].index(roaster)
    return {"coffee_id": f"{roaster}:{title}", "roaster": i, "title": title, "url": f"https://{roaster}/{i}",
            "origin_country": "Ethiopia", "process": process, "roast_level": "light", "tasting_notes": list(notes),
            "price_per_100g": 6.5, "min_price_usd": 22.0, "in_stock": in_stock, "catalog_status": "active",
            "first_seen": "2026-10-26"}  # fmt: skip


def catalog() -> dict:
    """620 coffees in stock from the Michigan roaster (past the 500-row page) and one or two from each other."""
    rows = [
        coffee("capitolbeans.com", "Capitol Roast"),
        coffee("drycreek.coffee", "Dry Creek Drip"),
        coffee("empire.coffee", "Empire Blend", notes=("vermont maple",)),
        coffee("lapaloma.coffee", "Paloma House"),
        coffee("littlehavana.coffee", "Miami Nights", notes=("mild", "cacao")),
        coffee("littlehavana.coffee", "Michigan Cherry"),
        coffee("maplecity.coffee", "Lakefront Blend", notes=("nutty",)),
    ]
    rows += [
        coffee("mitten.coffee", f"Mitten Cherry {i:03}" if i <= 20 else f"Mitten {i:03}",
               process="natural" if i <= 10 else "washed", notes=("mild",))
        for i in range(1, 621)
    ]  # fmt: skip
    rows += [
        coffee("mitten.coffee", "Mitten Old Crop", in_stock=False),
        coffee("northside.coffee", "Windy City Espresso"),
        coffee("ozark.coffee", "Ozark Morning"),
        coffee("sumatrahouse.coffee", "Indonesia Sumatra", notes=("dried fig", "made in small batches")),
    ]
    return {
        "release": {},
        "columns": COLUMNS,
        "rows": [[r[c] for c in COLUMNS] for r in rows],
        "roasters": {
            "columns": ["roaster_id", "roaster_name", "city", "state"],
            "rows": [list(r) for r in ROASTERS],
        },
    }


def render(out: Path) -> None:
    """The catalog page as site.build renders it, with empty charts and counts."""
    env = Environment(loader=PackageLoader("coffeedata", "templates"), autoescape=select_autoescape())
    html = env.get_template("index.html").render(
        links=site.links(), jsonld="", totals={"coffees": 0, "in_stock": 0, "roasters": 0}, status_rows=[],
        charts={"origin_price": "", "process": ""}, new=[], coverage={"registry": {}}, origin_price=[],
        processes=[], generated="2026-11-09 12:00 UTC", state_titles=site.STATE_TITLES, state_words=site.STATE_WORDS,
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
            ["node", str(BROWSER / "catalog_flow.js"), f"http://127.0.0.1:{server.server_port}", str(shots)],
            capture_output=True, text=True, timeout=300, cwd=BROWSER,
        )  # fmt: skip
    finally:
        server.shutdown()
        thread.join()
    assert r.returncode == 0, r.stderr[-3000:]
    assert {p.name for p in shots.iterdir()} == {"catalog-michigan-light.png", "catalog-michigan-dark.png"}


def test_state_words_read_the_longest_name_first():
    """ "west virginia" is read as one state before "virginia", and every code has a name to show."""
    names = [name for name, _ in site.STATE_WORDS]
    assert names.index("west virginia") < names.index("virginia")
    assert names.index("district of columbia") < names.index("washington")
    assert site.STATE_TITLES["DC"] == "District of Columbia" and site.STATE_TITLES["NC"] == "North Carolina"
    assert set(site.STATE_TITLES) == set(site.US_STATES)
