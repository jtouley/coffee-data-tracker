"""One real-browser run of Log shot -> Saved -> Undo at 390 px (tests/browser/log_shot.js). Needs Node with
Playwright; skipped where it isn't installed. APP_SCREENSHOTS=<dir> keeps the screenshots."""

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

from app.auth import LocalOwner
from app.main import create_app
from coffeedata import shots

HERE = Path(__file__).parent


def _playwright() -> bool:
    node = shutil.which("node")
    return (
        bool(node)
        and subprocess.run([node, "-e", "require('playwright')"], capture_output=True).returncode == 0
    )


pytestmark = pytest.mark.skipif(not _playwright(), reason="Node with Playwright not installed")


def test_log_shot_to_saved_and_undo(lake, tmp_path):
    app = create_app(lake, LocalOwner("owner", "jtouley"))
    c = TestClient(app)
    w = {"X-Requested-With": "test"}
    # A shot two days ago, so the saved screen has something to compare with.
    earlier = datetime.now(UTC).date() - timedelta(days=2)
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
            "coffee_freetext": "Hambela Alaka",
            "roast_date": str(earlier - timedelta(days=5)),
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

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    out = Path(os.environ.get("APP_SCREENSHOTS") or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["node", str(HERE / "browser" / "log_shot.js"), f"http://127.0.0.1:{port}", str(out)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        server.should_exit = True
        thread.join()
    assert r.returncode == 0, r.stderr[-3000:]
    shot_id = r.stdout.strip()
    # The browser's shot was saved, then retracted by Undo: two records, no current shot.
    assert len(list((lake.root / "shots").glob(f"{shot_id}__*.parquet"))) == 2
    assert [s.shot_id for s in shots.load_shots(lake)] == ["first"]
    assert {p.name for p in out.iterdir()} >= {"today.png", "log-shot.png", "saved.png"}
