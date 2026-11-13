"""The usage counting in a real browser (tests/browser/usage_flow.js): every page that sends events, on the site
(landing, catalog, privacy, terms, bot) and in the app (a coffee's Shop link, the bag screen's catalog search), at
390 px and desktop, light and dark, with axe and a screenshot each. It checks that nothing is sent while a page loads,
that events leave on a click out or when the page is hidden, that no request to the collector carries a cookie or
credentials (a cookie is set on the collector's host to prove it), that Global Privacy Control and Do Not Track stop
everything, that a slow or failing collector never holds a page up, and that links out carry the referral marker.
The collector is a local HTTPS server (Collector, below; Chromium maps e.registry.coffee to it), so what it records is
what really arrived, headers included: no request leaves 127.0.0.1. Needs openssl for its certificate.
USAGE_SCREENSHOTS=<dir> keeps the screenshots."""

import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE, Sessions
from app.main import create_app
from coffeedata import site

from .test_app_browser import BROWSER, PW, _accounts, _playwright, _serve
from .test_catalog_browser import BUILD
from .test_catalog_browser import render as render_catalog
from .test_smoke_web import QuietFiles

pytestmark = pytest.mark.skipif(
    not (_playwright() and shutil.which("openssl")) and not os.environ.get("REQUIRE_BROWSER"),
    reason="Node with Playwright and axe, or openssl, not installed",
)

RELEASE = {"release_build_id": BUILD}
NEW = {"coffee_id": "northside.coffee:Windy City Espresso", "roaster_id": "northside.coffee", "roaster_name": "Northside Coffee",
       "title": "Windy City Espresso", "url": "https://northside.coffee/products/windy?variant=2#top",
       "origin_country": "Ethiopia", "process": "washed", "price_per_100g": 6.5}  # fmt: skip


# The usage collector's wire schema (app/usage_events.py, Event, on the collector's branch): every name an event may
# carry, the surfaces, and the release's shape. The stand-in below reports any event the real one would drop.
FIELDS = {"event", "surface", "event_id", "release", "device", "query", "cities", "states", "origin", "process", "state",
          "in_stock", "sort", "results_n", "zero_results", "roaster_id", "coffee_id", "rank", "entry", "referrer",
          "utm_source", "utm_medium", "utm_campaign", "webdriver"}  # fmt: skip
SURFACES = {"landing", "catalog", "privacy", "terms", "bot", "app_search", "app_coffee"}
CLICK = ("roaster_id", "coffee_id", "rank")
SOURCE = ("referrer", "utm_source", "utm_medium", "utm_campaign")


def problems(e: dict) -> list[str]:
    """Why the collector would drop this event, if it would (a subset of its checks: names, surfaces, release,
    which event carries what)."""
    out = [f"unknown field {k}" for k in set(e) - FIELDS]
    if (
        e.get("event") not in {"page_view", "search", "filter_state", "click_out"}
        or e.get("surface") not in SURFACES
    ):
        out.append(f"event {e.get('event')} on {e.get('surface')}")
    if e.get("release") is not None and not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{6}", e["release"]):
        out.append(f"release {e['release']}")
    if e.get("event") == "click_out" and not e.get("roaster_id"):
        out.append("a click_out without its roaster")
    if e.get("event") != "click_out" and any(e.get(k) is not None for k in CLICK):
        out.append("a roaster, coffee or rank off a click_out")
    if not e.get("entry") and any(e.get(k) is not None for k in SOURCE):
        out.append("a source off the first event")
    if len(e.get("cities", [])) > 10 or len(e.get("states", [])) > 10 or len(e.get("query") or "") > 1000:
        out.append("over a limit")
    return out


class Collector:
    """A stand-in for the usage collector: HTTPS on one port (the browser's events), plain HTTP on another (the
    flow's control: POST /mode/<ok|slow|down> sets how it answers and forgets what it received; GET /seen lists every
    request since, with the headers that matter and its events). Requests are recorded as they arrive, before a slow
    answer."""

    def __init__(self, tmp: Path):
        self.mode, self.seen, self.lock = "ok", [], threading.Lock()
        key, cert = tmp / "key.pem", tmp / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                        str(cert), "-subj", "/CN=e.registry.coffee", "-days", "1"], check=True, capture_output=True)  # fmt: skip
        collector = self

        class Events(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    events = json.loads(body)["events"]
                except (ValueError, KeyError, TypeError):
                    events = [{"unreadable": body.decode(errors="replace")}]
                wrong = [p for e in events for p in problems(e)] + (
                    ["over 4 KB or 20 events"] if len(body) > 4096 or len(events) > 20 else []
                )
                with collector.lock:
                    collector.seen.append({"path": self.path, "method": "POST", "cookie": self.headers.get("cookie"),
                                           "auth": self.headers.get("authorization"), "origin": self.headers.get("origin"),
                                           "problems": wrong,
                                           "type": self.headers.get("content-type"), "events": events})  # fmt: skip
                    mode = collector.mode
                if mode == "down":
                    self.close_connection = True  # no answer at all
                    return
                if mode == "slow":
                    time.sleep(10)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        class Control(BaseHTTPRequestHandler):
            def do_POST(self):
                with collector.lock:
                    collector.mode, collector.seen = self.path.rsplit("/", 1)[1], []
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                with collector.lock:
                    data = json.dumps(collector.seen).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.https = ThreadingHTTPServer(("127.0.0.1", 0), Events)
        self.https.daemon_threads = True
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        self.https.socket = tls.wrap_socket(self.https.socket, server_side=True)
        self.control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        self.threads = [
            threading.Thread(target=s.serve_forever, daemon=True) for s in (self.https, self.control)
        ]
        for t in self.threads:
            t.start()

    def stop(self) -> None:
        for s in (self.https, self.control):
            s.shutdown()


def render_site(out: Path) -> None:
    """The landing page, the catalog and the three legal pages, as site.build renders them."""
    render_catalog(out)
    env = site.environment()
    landing = env.get_template("landing.html").render(
        totals={"coffees": 640, "in_stock": 620, "roasters": 15}, registry=40,
        reach={"origins": 1, "states": 13, "has_dc": True, "has_pr": False},
        week={"new": 1, "price_changes": 0, "removed": 0}, new=[NEW], charts={"origins": ""}, origin_counts=[],
        links=site.links("", None), public_tables=4, generated="November 9, 2026", jsonld="",
        usage=site.usage("landing", RELEASE),
    )  # fmt: skip
    (out / "index.html").write_text(site.with_csp(landing))
    site.copy_static(out)
    legal = {"links": site.links(), "updated": site.LEGAL_UPDATED, "contact": "support@registry.coffee",
             "governing_state": "Michigan"}  # fmt: skip
    for page in ("privacy", "terms", "bot"):
        (out / page).mkdir(exist_ok=True)
        extra = {"updated": site.BOT_UPDATED, "user_agent": "RegistryCoffeeBot"} if page == "bot" else {}
        html = env.get_template(f"{page}.html").render(**legal | extra, usage=site.usage(page, RELEASE))
        (out / page / "index.html").write_text(site.with_csp(html))


def seed_app(lake, tmp_path) -> tuple:
    """The app with one bag matched to a two-coffee catalog, and a signed-in session for the browser."""
    accounts = _accounts(tmp_path / "users")
    accounts.create_owner("SETUP-CODE-1234", "jtouley", PW, agree=True)
    app = create_app(lake, Sessions(accounts, secure=False))
    con = lake.connect()
    con.execute(
        "create table c as select * from (values"
        " ('example.com:1', 'example.com', 'Hambela Alaka', 'Larkspur Roasting', 'Ethiopia', 'washed', 6.47, true,"
        "  'https://example.com/products/hambela?size=250#buy'),"
        " ('example.com:2', 'example.com', 'Hambela Natural', 'Larkspur Roasting', 'Ethiopia', 'natural', 6.10, true,"
        "  'https://example.com/products/natural'))"
        " t(coffee_id, roaster_id, title, roaster_name, origin_country, process, price_per_100g, in_stock, url)"
    )
    lake.write_mart("dim_coffee", con, "select * from c")
    c = TestClient(app)
    w = {"X-Requested-With": "test", "X-Expected-User": "owner"}
    assert c.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=w).status_code == 200
    roasted = datetime.now(UTC).date() - timedelta(days=7)
    c.post("/api/bags", json={"bag_id": "bag1", "coffee_id": "example.com:1", "coffee_freetext": "Hambela Alaka",
                              "roast_date": str(roasted)}, headers=w)  # fmt: skip
    assert c.get("/coffee/bag1").status_code == 200
    return app, c.cookies.get(COOKIE)


def test_usage_events_in_a_browser(lake, tmp_path):
    render_site(tmp_path / "site")
    static = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietFiles, directory=str(tmp_path / "site")))
    thread = threading.Thread(target=static.serve_forever, daemon=True)
    thread.start()
    app, session = seed_app(lake, tmp_path)
    server, app_thread, port = _serve(app)
    collector = Collector(tmp_path)
    shots = Path(os.environ.get("USAGE_SCREENSHOTS") or tmp_path / "shots")
    shots.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["node", str(BROWSER / "usage_flow.js"), f"http://127.0.0.1:{static.server_port}",
             f"http://127.0.0.1:{port}", str(shots), json.dumps({"name": COOKIE, "value": session}), BUILD,
             str(collector.https.server_port), f"http://127.0.0.1:{collector.control.server_port}"],
            capture_output=True, text=True, timeout=600, cwd=BROWSER,
        )  # fmt: skip
    finally:
        static.shutdown()
        thread.join()
        server.should_exit = True
        app_thread.join()
        collector.stop()
    assert r.returncode == 0, r.stderr[-4000:]
    print(r.stdout)  # axe's lesser findings, for the record
    pages = ("landing", "catalog", "privacy", "terms", "bot", "app-coffee", "app-search")
    assert {p.name for p in shots.iterdir()} == {
        f"usage-{p}-{w}-{s}.png" for p in pages for w in ("390", "desktop") for s in ("light", "dark")
    }
