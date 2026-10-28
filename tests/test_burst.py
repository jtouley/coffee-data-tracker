"""Hardening for a burst of requests on the one Cloud Run instance: scanner paths stop at the outermost layer,
password hashing runs a few at a time, and a failed lake read at start is retried rather than crashing."""

import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import accounts as accounts_mod
from app import web
from app.main import create_app
from coffeedata import lake as lake_mod
from coffeedata.lake import Lake

from .test_app import HeaderAuth

PROBES = [
    "/.env",
    "/.git/config",
    "/.git/HEAD",
    "/.aws/credentials",
    "/.ssh/id_rsa",
    "/config/.env",
    "/app/.env.production",
    "/backup.sql",
    "/db.bak",
    "/config.yml",
    "/docker-compose.yaml",
    "/php.ini",
    "/wp-login.php",
    "/wp-admin/setup-config.php",
    "/index.php",
    "/cgi-bin/luci",
    "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php",
    "/phpmyadmin/",
    "/admin.aspx",
    "/.DS_Store",
]
REAL = [
    "/",
    "/log",
    "/log/gear",
    "/saved/s1",
    "/coffees",
    "/history",
    "/coffee/bag1",
    "/shot/s1",
    "/photos/p1",
    "/setup",
    "/setup/gear/g1",
    "/bag/new",
    "/signin",
    "/join",
    "/reset",
    "/invite",
    "/offline",
    "/health",
    "/healthz",
    "/sw.js",
    "/robots.txt",
    "/manifest.webmanifest",
    "/static/app.3f2a1c9b0d.css",
    "/static/fonts/archivo.ab12cd34ef.woff2",
    "/static/icons/icon-192.0123456789.png",
    "/api/shots",
    "/api/photos/p1",
    "/api/shots/s1/retract",
    "/.well-known/assetlinks.json",
]


@pytest.mark.parametrize("path", PROBES)
def test_scanner_paths_match(path):
    assert web.PROBE.search(path)


@pytest.mark.parametrize("path", REAL)
def test_real_routes_dont_match(path):
    assert not web.PROBE.search(path)


def test_every_real_route_is_left_alone(lake):
    app = create_app(lake, HeaderAuth())
    for route in app.routes:
        path = getattr(route, "path", "")
        assert not web.PROBE.search(path.replace("{", "").replace("}", "")), path
    c = TestClient(app)
    shell = re.findall(r'"(/static/[^"]+|/offline)"', c.get("/sw.js").text)  # the hashed files the app serves
    assert len(shell) > 3
    for path in shell + ["/sw.js", "/manifest.webmanifest", "/robots.txt", "/health"]:
        assert c.get(path).status_code == 200, path


def test_probes_get_an_empty_404_before_the_app(lake, caplog):
    c = TestClient(create_app(lake, HeaderAuth()))
    for path in PROBES:
        for method in ("GET", "POST", "HEAD"):
            r = c.request(method, path, headers={"accept": "text/html"})
            assert r.status_code == 404, path
            assert r.content == b""
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["x-content-type-options"] == "nosniff"  # still gets the security headers
    assert not [r for r in caplog.records if r.name == "app.access"]  # no access line per probe


def test_real_404s_still_come_from_the_app(lake):
    c = TestClient(create_app(lake, HeaderAuth()))
    r = c.get("/nothing-here", headers={"accept": "text/html"})
    assert r.status_code == 404 and b"<html" in r.content.lower()
    assert c.get("/health").json() == {"ok": True}


def test_hashing_runs_a_few_at_a_time(tmp_path, monkeypatch):
    users = Lake(str(tmp_path / "users"), tables=("users", "invites"))
    accts = accounts_mod.Accounts(users, "pepper-" + "p" * 16, "s" * 40, None)
    running, most, lock = 0, 0, threading.Lock()
    real = accounts_mod._hasher.verify

    def slow_verify(h, p):
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return real(h, p)

    class Slow:  # the real hasher's methods are read-only, so stand in for the whole hasher
        verify = staticmethod(slow_verify)

    monkeypatch.setattr(accounts_mod, "_hasher", Slow())
    threads = [threading.Thread(target=accts._verify, args=(accts._dummy, "guess")) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most == accounts_mod.HASHES_AT_ONCE


def test_a_failed_read_at_start_is_retried(lake, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(lake_mod.time, "sleep", sleeps.append)
    real, calls = Lake.connect, {"n": 0}

    class Flaky:
        def __init__(self, con):
            self.con = con

        def execute(self, *a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("HTTP 429")
            return self.con.execute(*a, **k)

    monkeypatch.setattr(Lake, "connect", lambda self, *a, **k: Flaky(real(self, *a, **k)))
    assert lake.latest("bags", "bag_id", attempts=3) == []
    assert sleeps == [2, 4]

    calls["n"], sleeps[:] = 0, []
    with pytest.raises(OSError):
        lake.latest("bags", "bag_id")  # one attempt by default: the weekly run is unchanged
    assert sleeps == []
