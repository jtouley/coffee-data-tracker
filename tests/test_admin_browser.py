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


def test_admin_in_a_browser(tmp_path):
    built = build_admin_lake(tmp_path / "built")
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
