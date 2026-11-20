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
from coffeedata.lake import USER_TABLES, Lake

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
    users = Lake(str(tmp_path / "users"), tables=USER_TABLES)
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


# --- shedding sign-ins past the hashing queue (security self-assessment L-5) ---------------------------------------


class Held:
    """Stands in for the hasher: each verify holds its slot until `go` is set, so the test decides when they end."""

    def __init__(self) -> None:
        self.go = threading.Event()
        self.started = 0
        self.lock = threading.Lock()

    def verify(self, h, p):
        with self.lock:
            self.started += 1
        assert self.go.wait(10)
        return False


def test_past_the_waiting_line_password_checks_are_turned_away_not_queued(tmp_path, monkeypatch):
    users = Lake(str(tmp_path / "users"), tables=USER_TABLES)
    accts = accounts_mod.Accounts(users, "pepper-" + "p" * 16, "s" * 40, None)
    gate = accounts_mod.HashGate(slots=2, waiters=3, wait=10.0)
    held = Held()
    monkeypatch.setattr(accounts_mod, "_hashing", gate)
    monkeypatch.setattr(accounts_mod, "_hasher", held)
    results: list[str] = []
    lock = threading.Lock()

    def attempt():
        try:
            accts._verify(accts._dummy, "guess")
            out = "checked"
        except accounts_mod.Busy:
            out = "busy"
        with lock:
            results.append(out)

    threads = [threading.Thread(target=attempt) for _ in range(2 + 3 + 4)]  # slots, waiters, then 4 more
    for t in threads:
        t.start()
    # The 4 past the line come back at once, while the 2 running and 3 waiting are still held.
    deadline = time.monotonic() + 5
    while (results.count("busy") < 4 or held.started < 2 or gate.waiting < 3) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert results == ["busy"] * 4 and held.started == 2 and gate.waiting == 3
    held.go.set()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["busy"] * 4 + ["checked"] * 5
    assert gate.waiting == 0 and held.started == 5


def test_a_waiter_that_waits_too_long_is_turned_away(tmp_path, monkeypatch):
    gate = accounts_mod.HashGate(slots=1, waiters=4, wait=0.05)
    with gate():
        t0 = time.monotonic()
        with pytest.raises(accounts_mod.Busy), gate():
            pass
        assert time.monotonic() - t0 < 2
    with gate():  # the slot is free again
        pass
    assert gate.waiting == 0


def test_the_real_hashing_parameters_are_unchanged():
    p = accounts_mod._hasher
    assert (p.time_cost, p.memory_cost, p.parallelism, p.type.name) == (3, 65536, 4, "ID")


def test_a_shed_sign_in_is_a_503_with_retry_after_and_counts_no_failure(lake, tmp_path, monkeypatch):
    from app.auth import Sessions

    users = Lake(str(tmp_path / "users"), tables=USER_TABLES)
    accts = accounts_mod.Accounts(users, "pepper-" + "p" * 16, "s" * 40, "K7QD-M2XF-9RPA-T4WE")
    c = TestClient(create_app(lake, Sessions(accts)), base_url="https://testserver", follow_redirects=False)
    w = {"X-Requested-With": "test", "X-Expected-User": "owner"}
    pw = "correct horse battery"
    body = {"agree": True, "setup_code": "K7QD-M2XF-9RPA-T4WE", "username": "jtouley", "password": pw}
    assert c.post("/api/signin", json=body, headers=w).status_code == 200
    c.cookies.clear()
    gate = accounts_mod.HashGate(slots=1, waiters=0, wait=10.0)
    monkeypatch.setattr(accounts_mod, "_hashing", gate)
    with gate():  # every slot taken, and no room to wait
        r = c.post("/api/signin", json={"username": "jtouley", "password": "wrong"}, headers=w)
    assert r.status_code == 503 and r.headers["retry-after"] == str(accounts_mod.RETRY_AFTER_S)
    assert r.json()["detail"] == accounts_mod.BUSY
    # Turned away isn't a wrong guess: no try was counted against the username or the address.
    assert not any(accts.limiter.fails.get(k) for k in list(accts.limiter.fails))
    # With a slot free, the same sign-in works.
    r = c.post("/api/signin", json={"username": "jtouley", "password": pw}, headers=w)
    assert r.status_code == 200


def _owner_accounts(tmp_path) -> "accounts_mod.Accounts":
    users = Lake(str(tmp_path / "users"), tables=USER_TABLES)
    accts = accounts_mod.Accounts(users, "pepper-" + "p" * 16, "s" * 40, "K7QD-M2XF-9RPA-T4WE")
    accts.create_owner("K7QD-M2XF-9RPA-T4WE", "jtouley", "correct horse battery", "198.51.100.1", agree=True)
    return accts


def test_a_checked_guess_is_always_counted_even_if_a_later_slot_would_be_refused(tmp_path, monkeypatch):
    """Both forms of a password (NFC, then as typed) are checked under one slot, so the gate can only refuse before
    any check runs. The review's reproduction: a gate that refuses every acquisition after the first, and a guess
    with a decomposed character (two forms). Each of three guesses was checked, so each is counted."""
    import contextlib

    accts = _owner_accounts(tmp_path)
    calls = {"n": 0}

    @contextlib.contextmanager
    def gate():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise accounts_mod.Busy
        yield

    monkeypatch.setattr(accounts_mod, "_hashing", gate)
    decomposed = "cafe\u0301 wrong guess here"
    for _ in range(3):
        calls["n"] = 0
        with pytest.raises(accounts_mod.AccountError):
            accts.sign_in("jtouley", decomposed, "203.0.113.9")
        assert calls["n"] == 1  # one slot for both forms
    assert len(accts.limiter.fails["ip:203.0.113.9"]) == 3
    assert len(accts.limiter.fails["u:jtouley"]) == 3
    # The same holds for re-authentication before a sensitive change.
    calls["n"] = 0
    user_id = accts.by_name("jtouley").user_id
    with pytest.raises(accounts_mod.AccountError):
        accts._reauth(user_id, decomposed)
    assert len(accts.limiter.fails[f"reauth:u:{user_id}"]) == 1


def test_the_waiting_line_is_first_come_first_served():
    """A slot freed while someone waits goes to them, not to a newcomer who arrives just after."""
    gate = accounts_mod.HashGate(slots=1, waiters=2, wait=0.3)
    entered: list[str] = []
    release = threading.Event()

    def first_in_line():
        with gate():
            entered.append("waiter")
            release.wait(5)

    holder = gate()
    holder.__enter__()
    t = threading.Thread(target=first_in_line)
    t.start()
    deadline = time.monotonic() + 5
    while gate.waiting < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    holder.__exit__(None, None, None)  # the slot passes to the waiter...
    with pytest.raises(accounts_mod.Busy), gate():  # ...so the newcomer waits, and runs out of time
        entered.append("newcomer")
    release.set()
    t.join(5)
    assert entered == ["waiter"] and gate.waiting == 0
    with gate():  # and the slot is free again
        pass


def test_a_right_password_is_never_answered_with_busy_by_the_rehash(tmp_path, monkeypatch):
    import contextlib

    accts = _owner_accounts(tmp_path)
    before = accts.by_name("jtouley").password_hash
    calls = {"n": 0}

    @contextlib.contextmanager
    def gate():
        calls["n"] += 1
        if calls["n"] >= 2:  # the check gets its slot; the rehash after it doesn't
            raise accounts_mod.Busy
        yield

    real = accounts_mod._hasher

    class NeedsRehash:
        verify = staticmethod(real.verify)
        hash = staticmethod(real.hash)

        @staticmethod
        def check_needs_rehash(h):
            return True

    monkeypatch.setattr(accounts_mod, "_hashing", gate)
    monkeypatch.setattr(accounts_mod, "_hasher", NeedsRehash())
    acct = accts.sign_in("jtouley", "correct horse battery", "203.0.113.9")
    assert acct.username == "jtouley" and accts.by_name("jtouley").password_hash == before
