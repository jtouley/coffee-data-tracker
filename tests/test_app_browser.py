"""The app in a real browser at 390 px (tests/browser/app_flow.js): owner setup, sign-in, Log shot -> Saved ->
Undo, axe on every screen in light and dark. Needs Node with Playwright and axe-core (`npm ci` in tests/browser,
or a global Playwright); skipped where they aren't installed, except in CI (REQUIRE_BROWSER=1), where a skip
would hide a failure. APP_SCREENSHOTS=<dir> keeps the screenshots."""

import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.auth import Sessions
from app.main import create_app
from coffeedata import shots
from coffeedata.lake import Lake

HERE = Path(__file__).parent
BROWSER = HERE / "browser"
PW = "kettle orbit marble"


def _playwright() -> bool:
    node = shutil.which("node")
    check = "require('playwright'); require.resolve('axe-core')"
    return (
        bool(node) and subprocess.run([node, "-e", check], cwd=BROWSER, capture_output=True).returncode == 0
    )


pytestmark = pytest.mark.skipif(
    not _playwright() and not os.environ.get("REQUIRE_BROWSER"), reason="Node with Playwright not installed"
)


def _accounts(root: Path) -> Accounts:
    users = Lake(str(root), tables=("users", "invites"))
    return Accounts(users, "browser-test-pepper", "browser-test-session-secret-0123456789", "SETUP-CODE-1234")


def _serve(app) -> tuple[uvicorn.Server, threading.Thread, int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread, port


def test_app_in_a_browser(lake, tmp_path):
    accounts = _accounts(tmp_path / "users")
    accounts.create_owner("SETUP-CODE-1234", "jtouley", PW)
    app = create_app(lake, Sessions(accounts, secure=False))  # plain http on 127.0.0.1
    fresh = create_app(
        Lake(str(tmp_path / "fresh-lake")), Sessions(_accounts(tmp_path / "fresh-users"), secure=False)
    )
    c = TestClient(app)
    w = {"X-Requested-With": "test"}
    assert c.post("/api/signin", json={"username": "jtouley", "password": PW}, headers=w).status_code == 200
    # A bag matched to a (one-row) catalog, with a few days of shots, so the screenshots look like real use. The
    # latest, two days ago, is what the saved screen compares with.
    earlier = datetime.now(UTC).date() - timedelta(days=2)
    con = lake.connect()
    con.execute(
        "create table c as select 'example.com:1' coffee_id, 'Hambela Alaka' title, 'Larkspur Roasting' roaster_name,"
        " 'Ethiopia' origin_country, 'washed' process"
    )
    lake.write_mart("dim_coffee", con, "select * from c")
    for body in (
        {
            "equipment_id": "g1",
            "kind": "grinder",
            "name": "DF64 V2",
            "scale_min": 0,
            "scale_max": 90,
            "scale_step": 0.5,
        },
        {"equipment_id": "b1", "kind": "basket", "name": "Pullman 18 g", "basket_dose_g": 18},
    ):
        c.post("/api/equipment", json=body, headers=w)
    c.post(
        "/api/bags",
        json={
            "bag_id": "bag1",
            "coffee_id": "example.com:1",
            "coffee_freetext": "Hambela Alaka",
            "roast_date": str(earlier - timedelta(days=5)),
        },
        headers=w,
    )
    for i, (t, taste, grind, rating) in enumerate(
        [(21, "sour", 12, 2), (24, "sour", 11.5, 3), (34, "bitter", 10, 2), (27, "balanced", 10.5, 4)]
    ):
        day = earlier - timedelta(days=2 - i // 2)
        c.post(
            "/api/shots",
            json={
                "shot_id": f"dial{i}",
                "brewed_at": f"{day}T{7 + i % 2 * 7:02d}:0{i}:00Z",
                "bag_id": "bag1",
                "grinder_id": "g1",
                "basket_id": "b1",
                "dose_g": 18,
                "yield_g": [35.0, 39.0, 41.0, 36.8][i],
                "time_s": t,
                "grinder_setting": grind,
                "rating": rating,
                "taste": taste,
            },
            headers=w,
        )
    c.post(
        "/api/shots",
        json={
            "shot_id": "first",
            "brewed_at": f"{earlier}T14:04:00Z",
            "bag_id": "bag1",
            "grinder_id": "g1",
            "basket_id": "b1",
            "dose_g": 18,
            "yield_g": 36.4,
            "time_s": 29,
            "grinder_setting": 11,
            "rating": 4,
            "taste": "balanced",
        },
        headers=w,
    )

    servers = [_serve(fresh), _serve(app)]
    out = Path(os.environ.get("APP_SCREENSHOTS") or tmp_path / "shots")
    out.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            [
                "node",
                str(BROWSER / "app_flow.js"),
                *(f"http://127.0.0.1:{p}" for _, _, p in servers),
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=BROWSER,
        )
    finally:
        for server, thread, _ in servers:
            server.should_exit = True
            thread.join()
    assert r.returncode == 0, r.stderr[-3000:]
    shot_id = r.stdout.strip()
    # The browser's shot was saved, then retracted by Undo: two records, no current shot.
    assert len(list((lake.root / "shots").glob(f"{shot_id}__*.parquet"))) == 2
    assert [s.shot_id for s in shots.load_shots(lake)][-1] == "first"
    assert all(s.shot_id != shot_id for s in shots.load_shots(lake))
    screens = ("owner-setup", "first-run", "setup-empty", "new-bag", "offline", "not-found", "sign-in")
    screens += ("sign-in-error", "today", "log-shot", "saved", "setup")
    assert {p.name for p in out.iterdir()} >= {f"{n}-{m}.png" for n in screens for m in ("light", "dark")}
