"""Week's crawl health in a real browser (tests/browser/admin_health.js) over the synthetic lake: this week with a
died attempt, a clean week, last week, no weeks at all, a week with fetches but no jobs, a slow lake (the cup, then
the data), a broken one, and the stores that keep failing with their Test crawl while writes are on. Every state at
390 px and 1,280 px, light and dark, with axe and no sideways scroll at 390 px.

Needs Node with Playwright and axe-core (`npm ci` in tests/browser) and a local Postgres (the writes-on app); skipped
where they aren't installed, except in CI (REQUIRE_BROWSER=1). ADMIN_SCREENSHOTS=<dir> keeps the screenshots."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import duckdb
import pytest

from admin.main import Reader, create_app
from admin.store import FileStore
from coffeedata.lake import Lake

from .admin_health_fixture import NOW, W0, W1, W2, W3, build_health_lake, drop_connector
from .test_admin_browser import BROWSER, _port, _serve, _settings
from .test_app_browser import _playwright

pytestmark = pytest.mark.skipif(
    not _playwright() and not os.environ.get("REQUIRE_BROWSER"), reason="Node with Playwright not installed"
)


def test_week_health_in_a_browser(tmp_path, pg_server):
    from admin.controlplane import ControlPlane, url_connect

    from .admin_fakes import FakeJobs

    lake = tmp_path / "lake"
    build_health_lake(lake)
    before = sorted((p.relative_to(lake).as_posix(), p.stat().st_mtime_ns) for p in lake.rglob("*"))
    empty_lake = tmp_path / "empty-lake"
    empty_lake.mkdir()
    gone = tmp_path / "gone-lake"  # woocommerce never ran this week
    shutil.copytree(lake, gone)
    drop_connector(gone, W3, "woocommerce")
    ports = {k: _port() for k in ("main", "act", "empty", "slow", "broken", "gone")}

    def app(name, lake_dir=lake, **kw):
        return create_app(_settings(tmp_path / name, f"http://127.0.0.1:{ports[name]}", lake_dir),
                          now=lambda: NOW, **kw)  # fmt: skip

    def broken_lake():
        raise duckdb.IOException("HTTP 503 from the lake")

    act_settings = _settings(tmp_path / "act", f"http://127.0.0.1:{ports['act']}", lake)
    control = ControlPlane(url_connect(pg_server.database()))
    act_settings.control, act_settings.jobs = control, FakeJobs()
    act_settings.snapshots, act_settings.writes_off = FileStore(tmp_path / "act" / "snapshots"), []
    apps = {
        "main": app("main"),
        "act": create_app(act_settings, now=lambda: NOW),
        "empty": app("empty", empty_lake),
        "gone": app("gone", gone),
        "slow": app(
            "slow", reader=Reader(Lake(str(lake), read_only=True), 30, before=lambda: time.sleep(3)), slow_s=1
        ),
        "broken": app("broken", reader=Reader(Lake(str(lake), read_only=True), 30, before=broken_lake)),
    }
    out = Path(os.environ.get("ADMIN_SCREENSHOTS") or tmp_path / "shots").resolve() / "health"
    out.mkdir(parents=True, exist_ok=True)
    config = {"out": str(out), "weeks": [W0, W1, W2, W3],
              **{k: f"http://127.0.0.1:{p}" for k, p in ports.items()},
              "cookies": {k: a.state.auth.start_session("browser-test") for k, a in apps.items()}}  # fmt: skip
    (tmp_path / "config.json").write_text(json.dumps(config))
    servers = [_serve(a, ports[k]) for k, a in apps.items()]
    try:
        r = subprocess.run(["node", str(BROWSER / "admin_health.js"), str(tmp_path / "config.json")],
                           capture_output=True, text=True, timeout=600, cwd=BROWSER)  # fmt: skip
    finally:
        for server, thread in servers:
            server.should_exit = True
            thread.join()
        control.close()
    print(r.stdout[-6000:])
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    after = sorted((p.relative_to(lake).as_posix(), p.stat().st_mtime_ns) for p in lake.rglob("*"))
    assert after == before  # the admin never wrote to the lake
