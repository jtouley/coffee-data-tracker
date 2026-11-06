"""Smoke, web (`make smoke`; CI's smoke job, part web). The static site built from the fixture lake's served release
(the path `coffeedata site --served` takes), with non-zero counts; then the app started as `make app` starts it
(uvicorn, over the local lake, with sign-in on) and one Playwright pass at 390 px (tests/browser/smoke_flow.js):
landing, catalog, the join page, and a seeded user's sign-in. Needs dbt, and Node with Playwright: skipped without
them, except under REQUIRE_DBT and REQUIRE_BROWSER (CI's smoke job), where a missing one fails."""

import json
import os
import re
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
from coffeedata.lake import USER_TABLES, Lake

from .smoke import BLACK_HOLE, PROXY_VARS, fixture_lake, require, require_dbt
from .test_app_browser import BROWSER, _free_port, _playwright

ROOT = Path(__file__).parent.parent
USER, PW = "smoke-owner", "kettle orbit marble"
PEPPER, SECRET, SETUP = (
    "smoke-test-pepper-0123",
    "smoke-test-session-secret-0123456789abcdef",
    "SMOKE-SETUP-1",
)

pytestmark = pytest.mark.smoke


class QuietFiles(SimpleHTTPRequestHandler):
    """The built site's files, without a log line per request."""

    def log_message(self, *a):
        pass


def test_site_and_app_in_a_browser(lake, tmp_path, monkeypatch, no_network):
    require_dbt()
    require("Node with Playwright", _playwright(axe=False), "REQUIRE_BROWSER")  # this pass runs no axe
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
    Accounts(Lake(str(users), tables=USER_TABLES), PEPPER, SECRET, SETUP).create_owner(
        SETUP, USER, PW, agree=True
    )
    static = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietFiles, directory=str(site)))
    threading.Thread(target=static.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env |= {k: BLACK_HOLE for k in PROXY_VARS} | {
        "COFFEEDATA_LAKE": str(lake.root), "COFFEEDATA_USERS": str(users), "AUTH_PEPPER": PEPPER,
        "SESSION_SECRET": SECRET, "COFFEEDATA_INSECURE_COOKIES": "1",  # plain http on 127.0.0.1
    }  # fmt: skip
    log = tmp_path / "app.log"
    shots = Path(os.environ.get("APP_SCREENSHOTS") or tmp_path / "shots")
    shots.mkdir(parents=True, exist_ok=True)
    app, port = start_app(env, log)
    try:
        r = subprocess.run(
            ["node", str(BROWSER / "smoke_flow.js"), f"http://127.0.0.1:{static.server_address[1]}",
             f"http://127.0.0.1:{port}", str(shots), USER, PW],
            capture_output=True, text=True, timeout=120, cwd=BROWSER,
        )  # fmt: skip
    finally:
        app.terminate()
        app.wait(timeout=10)
        static.shutdown()
        static.server_close()
    assert r.returncode == 0, r.stderr[-3000:] + "\n--- app log ---\n" + log.read_text()[-2000:]
    assert {p.name for p in shots.iterdir()} >= {"landing.png", "catalog.png", "join.png", "signed-in.png"}


def start_app(env: dict, log: Path, tries: int = 2) -> tuple[subprocess.Popen, int]:
    """uvicorn app.main:app on a free port, its output in `log` (a file, so a chatty app never blocks on a full
    pipe). The port is free when picked but can be taken before uvicorn binds it: that collision is retried once."""
    for attempt in range(tries):
        port = _free_port()
        with log.open("a") as out:
            app = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
            )  # fmt: skip
        if wait_for(f"http://127.0.0.1:{port}/healthz", app):
            return app, port
        text = log.read_text()
        if "address already in use" not in text.lower() or attempt == tries - 1:
            raise AssertionError(f"the app didn't start:\n{text[-2000:]}")
    raise AssertionError("unreachable")


def wait_for(url: str, proc: subprocess.Popen, timeout: float = 30) -> bool:
    """True once `url` answers 200; False if the process exits first. Raises if neither happens in time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if httpx.get(url, timeout=1, trust_env=False).status_code == 200:
                return True
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise AssertionError(f"the app didn't answer {url} in {timeout} s")
