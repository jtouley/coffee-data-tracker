"""Smoke, web (`make smoke`; CI's smoke job, part web). The static site built from the fixture lake's served release
(the path `coffeedata site --served` takes), with non-zero counts; then the app started as `make app` starts it
(uvicorn, over the local lake, with sign-in on) and one Playwright pass at 390 px (tests/browser/smoke_flow.js):
landing, catalog, the join page, and a seeded user's sign-in. Needs dbt, and Node with Playwright (skipped without
them, except under REQUIRE_BROWSER=1, as CI sets)."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.accounts import Accounts
from coffeedata import cli, connectors
from coffeedata.lake import Lake

from .smoke import BLACK_HOLE, PROXY_VARS, fixture_lake

ROOT = Path(__file__).parent.parent
BROWSER = Path(__file__).parent / "browser"
USER, PW = "smoke-owner", "kettle orbit marble"
PEPPER, SECRET, SETUP = (
    "smoke-test-pepper-0123",
    "smoke-test-session-secret-0123456789abcdef",
    "SMOKE-SETUP-1",
)

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(shutil.which("dbt") is None, reason="dbt not installed"),
]


class QuietFiles(SimpleHTTPRequestHandler):
    """The built site's files, without a log line per request."""

    def log_message(self, *a):
        pass


def _playwright() -> bool:
    """Node can load Playwright (tests/browser's, after `npm ci`, or a global one); this pass needs no axe."""
    node = shutil.which("node")
    check = (
        subprocess.run([node, "-e", "require('playwright')"], cwd=BROWSER, capture_output=True)
        if node
        else None
    )
    return bool(check and check.returncode == 0)


def test_site_and_app_in_a_browser(lake, tmp_path, monkeypatch, no_network):
    if not _playwright():
        if os.environ.get("REQUIRE_BROWSER"):
            pytest.fail("REQUIRE_BROWSER is set, but Node with Playwright isn't installed")
        pytest.skip("Node with Playwright not installed")
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path / "dbt-target"))
    monkeypatch.setenv("DBT_LOG_PATH", str(tmp_path / "dbt-logs"))
    monkeypatch.setattr(cli, "DBT_TARGET", tmp_path / "dbt-target")
    monkeypatch.setattr(cli, "SERVED_DB", tmp_path / "served.duckdb")
    week = connectors.week_of(cli._today())
    fixture_lake(lake, week)
    cli.main(["build", "--db", str(tmp_path / "coffee.duckdb"), "--run-date", week])

    # The site, from the served release only (the site workflow's command).
    site = tmp_path / "site"
    cli.main(["site", "--served", "--db", str(tmp_path / "site.duckdb"), "--out", str(site)])
    landing, catalog = (site / "index.html").read_text(), (site / "catalog" / "index.html").read_text()
    counts = re.findall(r'<div class="n">([\d,]+)</div>', landing)
    assert len(counts) >= 4 and all(int(n.replace(",", "")) > 0 for n in counts), counts
    assert '<h1 class="serif">The catalog</h1>' in catalog
    assert json.loads((site / "catalog" / "catalog.json").read_text())["rows"]

    # A seeded user in the private users lake, then the app as `make app` runs it, with sign-in on.
    users = tmp_path / "users"
    Accounts(Lake(str(users), tables=("users", "invites")), PEPPER, SECRET, SETUP).create_owner(
        SETUP, USER, PW
    )
    static = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietFiles, directory=str(site)))
    threading.Thread(target=static.serve_forever, daemon=True).start()
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env |= {k: BLACK_HOLE for k in PROXY_VARS} | {
        "COFFEEDATA_LAKE": str(lake.root), "COFFEEDATA_USERS": str(users), "AUTH_PEPPER": PEPPER,
        "SESSION_SECRET": SECRET, "COFFEEDATA_INSECURE_COOKIES": "1",  # plain http on 127.0.0.1
    }  # fmt: skip
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    shots = Path(os.environ.get("APP_SCREENSHOTS") or tmp_path / "shots")
    shots.mkdir(parents=True, exist_ok=True)
    try:
        wait_for(f"http://127.0.0.1:{port}/healthz", app)
        r = subprocess.run(
            ["node", str(BROWSER / "smoke_flow.js"), f"http://127.0.0.1:{static.server_address[1]}",
             f"http://127.0.0.1:{port}", str(shots), USER, PW],
            capture_output=True, text=True, timeout=120, cwd=BROWSER,
        )  # fmt: skip
    finally:
        app.terminate()
        log = app.communicate(timeout=10)[0]
        static.shutdown()
        static.server_close()
    assert r.returncode == 0, r.stderr[-3000:] + "\n--- app log ---\n" + log[-2000:]
    assert {p.name for p in shots.iterdir()} >= {"landing.png", "catalog.png", "join.png", "signed-in.png"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"the app exited: {proc.communicate()[0][-2000:]}")
        try:
            if httpx.get(url, timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"the app didn't answer {url} in {timeout} s")
