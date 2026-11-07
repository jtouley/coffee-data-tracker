"""The admin in a real browser (tests/browser/admin_flow.js), with WebAuthn virtual authenticators for the passkeys:
signed out, enrollment (a wrong token, then the right one), the second passkey, sign-in with each, an expired session
and a cookie from another secret, recovery, every page with data, with no data, with a slow lake and with a broken
one, a service whose session secret is too short (it must fail closed), and search and filters. Every screen at
390 px and 1,280 px, light and dark, with axe.

Needs Node with Playwright and axe-core (`npm ci` in tests/browser) and dbt (the test lake is built). Skipped where
they aren't installed, except in CI (REQUIRE_BROWSER=1). ADMIN_SCREENSHOTS=<dir> keeps the screenshots."""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import duckdb
import pytest
import uvicorn

from admin.config import Settings
from admin.main import Reader, create_app
from admin.store import FileStore
from coffeedata.lake import Lake

from .admin_fixture import build_admin_lake
from .test_app_browser import _playwright

BROWSER = Path(__file__).parent / "browser"
TOKEN = "enroll-token-for-the-browser-test-0123456789"
SECRET = "session-secret-for-the-browser-test-0123456789"

pytestmark = pytest.mark.skipif(
    not (_playwright() and shutil.which("dbt")) and not os.environ.get("REQUIRE_BROWSER"),
    reason="Node with Playwright, or dbt, not installed",
)


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread


def _settings(root: Path, origin: str, lake: Path, secret: str = SECRET) -> Settings:
    return Settings(origin=origin, session_secret=secret, enroll_token=TOKEN, lake_url=str(lake),
                    cache_dir=root / "cache", store=FileStore(root / "state")).check()  # fmt: skip


def _expired_cookie(app) -> str:
    """A session that was real and is now past its 12 hours."""
    auth = app.state.auth
    cookie = auth.start_session("expired")
    sid = cookie.split(".")[0]
    key = __import__("hashlib").sha256(sid.encode()).hexdigest()
    path = auth.store.root / "sessions" / key
    rec = json.loads(path.read_text())
    path.write_text(
        json.dumps(
            rec | {"created_at": rec["created_at"] - 13 * 3600, "expires_at": rec["created_at"] - 3600}
        )
    )
    auth.sessions.clear()
    return cookie


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The test lake, built once through `coffeedata build` for both browser runs."""
    return build_admin_lake(tmp_path_factory.mktemp("built"))


def test_admin_in_a_browser(tmp_path, built):
    lake = built["lake"]
    before = sorted((p.relative_to(lake).as_posix(), p.stat().st_mtime_ns) for p in lake.rglob("*"))

    ports = {k: _port() for k in ("main", "off", "empty", "slow", "broken")}
    main = create_app(_settings(tmp_path / "main", f"http://localhost:{ports['main']}", lake))
    off = create_app(
        _settings(tmp_path / "off", f"http://127.0.0.1:{ports['off']}", lake, secret="too-short")
    )
    empty_lake = tmp_path / "empty-lake"
    empty_lake.mkdir()
    empty = create_app(_settings(tmp_path / "empty", f"http://127.0.0.1:{ports['empty']}", empty_lake))
    slow_root = tmp_path / "slow"
    slow = create_app(
        _settings(slow_root, f"http://127.0.0.1:{ports['slow']}", lake),
        reader=Reader(Lake(str(lake), read_only=True), 30, before=lambda: time.sleep(6)),
        slow_s=1,
    )

    def broken_lake():
        raise duckdb.IOException("HTTP 503 from the lake")

    broken = create_app(
        _settings(tmp_path / "broken", f"http://127.0.0.1:{ports['broken']}", lake),
        reader=Reader(Lake(str(lake), read_only=True), 30, before=broken_lake),
    )
    foreign = create_app(_settings(tmp_path / "foreign", f"http://localhost:{ports['main']}", lake,
                                   secret="another-secret-entirely-0123456789abcdef"))  # fmt: skip
    out = Path(os.environ.get("ADMIN_SCREENSHOTS") or tmp_path / "shots").resolve()
    out.mkdir(parents=True, exist_ok=True)
    w1, w2, w3 = built["weeks"]
    config = {
        "out": str(out),
        "main": f"http://localhost:{ports['main']}",
        "off": f"http://127.0.0.1:{ports['off']}",
        "empty": f"http://127.0.0.1:{ports['empty']}",
        "slow": f"http://127.0.0.1:{ports['slow']}",
        "broken": f"http://127.0.0.1:{ports['broken']}",
        "token": TOKEN,
        "weeks": [w1, w2, w3],
        "builds": [built["builds"][w1], built["builds"][w2]],
        "cookies": {
            "empty": empty.state.auth.start_session("test"),
            "slow": slow.state.auth.start_session("test"),
            "broken": broken.state.auth.start_session("test"),
            "expired": _expired_cookie(main),
            "foreign": foreign.state.auth.start_session("test"),
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    servers = [_serve(app, ports[k]) for k, app in
               (("main", main), ("off", off), ("empty", empty), ("slow", slow), ("broken", broken))]  # fmt: skip
    try:
        r = subprocess.run(["node", str(BROWSER / "admin_flow.js"), str(tmp_path / "config.json")],
                           capture_output=True, text=True, timeout=600, cwd=BROWSER)  # fmt: skip
    finally:
        for server, thread in servers:
            server.should_exit = True
            thread.join()
    print(r.stdout[-6000:])
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    # The admin never wrote to the lake: every file and its time are as they were.
    after = sorted((p.relative_to(lake).as_posix(), p.stat().st_mtime_ns) for p in lake.rglob("*"))
    assert after == before
    assert not any(empty_lake.iterdir())


class _Control:
    """The browser test's handle on the fake Cloud Run (tests/admin_fakes.py): GET /finish?job=&status=&message=,
    /refuse?job= and /down?on=1|0, on a port of its own. Test-only: the admin has no such route."""

    def __init__(self, fakes: dict):
        import http.server
        from urllib.parse import parse_qs, urlsplit

        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake = fakes[q.get("app", "act")]
                if u.path == "/finish":
                    fake.finish(q.get("job") or None, q.get("status", "succeeded"), q.get("message"))
                elif u.path == "/refuse":
                    fake.refuse = {q["job"]} if q.get("job") else set()
                elif u.path == "/down":
                    fake.down = q.get("on") == "1"
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", _port()), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        del outer

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()


def test_admin_actions_in_a_browser(tmp_path, built, pg_server):
    """The writes in a real browser over the real built lake (a copy, which only the fake pipeline writes): every
    job state (confirm, running, success, failure, cancel, a refusal, a double submit, the window override, Cloud Run
    refusing), Catalog decisions with Undo and a group, a draft built, Publish and Roll back, Discard, and writes off,
    a control plane that's down, a slow Cloud Run, CSRF and a signed-out write. Postgres is the local test server."""
    from datetime import UTC, datetime

    from admin.controlplane import ControlPlane, url_connect

    from .admin_fakes import FakeJobs, Pipeline

    from .admin_fixture import flag_served_build

    lake = tmp_path / "lake"
    shutil.copytree(built["lake"], lake)
    w1, w2, w3 = built["weeks"]
    flag_served_build(lake, w1, built["builds"][w1])
    fakes = {k: FakeJobs() for k in ("act", "window", "slow")}
    fakes["slow"].delay_s = 3
    fakes["act"].on_finish = Pipeline(lake, tmp_path / "act" / "snapshots", w3)
    ports = {k: _port() for k in ("act", "window", "off", "down", "slow")}

    def app(name, fake=None, control=None, now=None):
        st = _settings(tmp_path / name, f"http://127.0.0.1:{ports[name]}", lake)
        if fake is not None:
            st.control = control or ControlPlane(url_connect(pg_server.database()))
            st.jobs, st.snapshots, st.writes_off = fake, FileStore(tmp_path / name / "snapshots"), []
        return create_app(st, now=now)

    monday = datetime.fromisoformat(w3).replace(hour=10, tzinfo=UTC)
    apps = {
        "act": app("act", fakes["act"]),
        "window": app("window", fakes["window"], now=lambda: monday),
        "off": app("off"),
        "down": app("down", FakeJobs(), ControlPlane(url_connect("postgresql://nobody@127.0.0.1:1/none"))),
        "slow": app("slow", fakes["slow"]),
    }
    before = sorted(p.relative_to(built["lake"]).as_posix() for p in built["lake"].rglob("*"))
    out = Path(os.environ.get("ADMIN_SCREENSHOTS") or tmp_path / "shots").resolve() / "actions"
    out.mkdir(parents=True, exist_ok=True)
    with _Control(fakes) as ctl:
        config = {"out": str(out), "control": ctl.url, "weeks": [w1, w2, w3],
                  **{k: f"http://127.0.0.1:{p}" for k, p in ports.items()},
                  "cookies": {k: a.state.auth.start_session("browser-test") for k, a in apps.items()}}  # fmt: skip
        (tmp_path / "config.json").write_text(json.dumps(config))
        servers = [_serve(a, ports[k]) for k, a in apps.items()]
        try:
            r = subprocess.run(["node", str(BROWSER / "admin_actions.js"), str(tmp_path / "config.json")],
                               capture_output=True, text=True, timeout=900, cwd=BROWSER)  # fmt: skip
        finally:
            for server, thread in servers:
                server.should_exit = True
                thread.join()
    print(r.stdout[-8000:])
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-4000:]
    # the admin wrote nothing to the lake it read from; only the fake pipeline wrote its copy
    assert sorted(p.relative_to(built["lake"]).as_posix() for p in built["lake"].rglob("*")) == before
