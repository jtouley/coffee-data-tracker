"""The scheduler, simulated: Design C's week (config/connectors.toml) against the mock, one process per job.

usage: runner.py SCENARIO OUTDIR [PORT]
Jobs run one at a time at their scheduled simulated times (the clock jumps between them; jobs whose windows
would overlap share no rate limit except Shopify's, and only Shopify jobs and the sweep send Shopify traffic).
Day 5's Shopify shard is SIGKILLed at 50% progress and started again a minute later (a Cloud Run task retry).
Every job is killed at its deadline_min (240) if still running.
"""

import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from datetime import date, timedelta
from pathlib import Path

import httpx

SCEN, OUTD = sys.argv[1], Path(sys.argv[2])
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8870
WT = Path("/home/user/coffee-data-tracker/build/arch-compare/C/wt")
SIM = Path(__file__).resolve().parent
PY = "/home/user/coffee-data-tracker/.venv/bin/python"
PROFILES = "/tmp/archC/profiles.json"
FETCH_SCALE, SWEEP_SCALE = 10.0, 10.0  # the sweep skips its idle waits instead (job.py SkipTime)
KILL_DAY = 4  # day 5 of the week (0-based 4)

cfg = tomllib.loads((WT / "config/connectors.toml").read_text())
OUTD.mkdir(parents=True, exist_ok=True)
env = os.environ | {
    "COFFEEDATA_LAKE": str(OUTD / "lake"),
    "COFFEEDATA_RAW": str(OUTD / "bucket"),  # holds raw/ and control/
    "PYTHONPATH": "src:.",
    "UV_NO_SYNC": "1",
    "HF_TOKEN": "",
}
srv = subprocess.Popen([PY, str(SIM / "mock_c.py"), PROFILES, str(PORT), SCEN],
                       stdout=open(OUTD / "mock.log", "w"), stderr=subprocess.STDOUT)
for _ in range(100):
    try:
        httpx.get(f"http://127.0.0.1:{PORT}/__stats", timeout=1)
        break
    except httpx.HTTPError:
        time.sleep(0.2)


def stats() -> dict:
    return httpx.get(f"http://127.0.0.1:{PORT}/__stats", timeout=10).json()


# The schedule: (sim seconds since Monday 00:00, step, connector)
events = []
for d in range(7):
    for name, c in cfg["connectors"].items():
        if c["schedule"] == "daily_shard" or c.get("weekday") == d:
            events.append((d * 86400 + c["hour"] * 3600, "connector", name))
    events.append((d * 86400 + cfg["sweep"]["hour"] * 3600, "sweep", ""))
    events.append((d * 86400 + cfg["html_fallback"]["hour"] * 3600, "html", ""))
events.append((cfg["merge"]["weekday"] * 86400 + cfg["merge"]["hour"] * 3600, "merge", ""))
events.append((cfg["enrich"]["weekday"] * 86400 + cfg["enrich"]["hour"] * 3600, "enrich", ""))
events.sort()


def run_job(t0: float, step: str, conn: str, tag: str, kill_at: float | None = None) -> dict:
    day = int(t0 // 86400)
    run_date = str(date(2026, 11, 2) + timedelta(days=day))
    scale = SWEEP_SCALE if step in ("sweep", "html", "merge", "enrich") else FETCH_SCALE
    httpx.get(f"http://127.0.0.1:{PORT}/__clock", params={"t": t0, "s": scale})
    before = stats()
    out, prog = OUTD / f"jobs/{tag}.json", OUTD / f"jobs/{tag}.progress.json"
    out.parent.mkdir(exist_ok=True)
    prog.unlink(missing_ok=True)
    deadline = (cfg["connectors"].get(conn) or cfg["sweep"]).get("deadline_min", 240) * 60
    p = subprocess.Popen([PY, str(SIM / "job.py"), step, conn or "-", run_date, str(PORT), str(t0), str(scale),
                          str(out), str(prog)], cwd=WT, env=env,
                         stdout=open(OUTD / f"jobs/{tag}.log", "w"), stderr=subprocess.STDOUT)
    m0, killed, timed_out = time.monotonic(), False, False
    while p.poll() is None:
        time.sleep(0.05)
        el = (time.monotonic() - m0) * scale
        if kill_at is not None and prog.exists():
            try:
                pr = json.loads(prog.read_text())
            except ValueError:
                continue
            if pr["total"] and pr["done"] >= kill_at * pr["total"]:
                p.send_signal(signal.SIGKILL)
                killed = True
        if el > deadline:
            p.send_signal(signal.SIGKILL)
            timed_out = True
    p.wait()
    wall = (time.monotonic() - m0) * scale
    after = stats()
    res = json.loads(out.read_text()) if out.exists() else {}
    if "sim_end_s" in res:  # the job's own simulated span (includes the sweep's skipped waits)
        wall = res["sim_end_s"] - t0
    req = {a: after["by_adapter"].get(a, {}).get("req", 0) - before["by_adapter"].get(a, {}).get("req", 0)
           for a in after["by_adapter"]}
    r429 = {a: after["by_adapter"][a]["429"] - before["by_adapter"].get(a, {}).get("429", 0)
            for a in after["by_adapter"]}
    rec = {"job": tag, "step": step, "connector": conn, "run_date": run_date, "sim_start_h": round(t0 / 3600, 2),
           "wall_min": round(wall / 60, 1), "finished": p.returncode == 0 and not killed and not timed_out,
           "killed_at_50pct": killed, "timed_out": timed_out, "within_limit": wall <= deadline,
           "requests": {k: v for k, v in req.items() if v}, "http_429": {k: v for k, v in r429.items() if v},
           "ip_wide_429": after["429_ip"] - before["429_ip"], "woo_503": after["503_woo"] - before["503_woo"],
           "result": res}
    print(json.dumps({k: rec[k] for k in ("job", "wall_min", "finished", "requests", "http_429")}), flush=True)
    return rec


jobs = []
for t0, step, conn in events:
    day = int(t0 // 86400)
    tag = f"d{day + 1}-{step}{'-' + conn if conn else ''}"
    if step == "connector" and conn == "shopify" and day == KILL_DAY:
        first = run_job(t0, step, conn, tag + "-killed", kill_at=0.5)
        jobs.append(first)
        t1 = t0 + first["wall_min"] * 60 + 60
        jobs.append(run_job(t1, step, conn, tag + "-resume"))
    else:
        jobs.append(run_job(t0, step, conn, tag))
json.dump({"scenario": SCEN, "jobs": jobs, "mock": stats()}, open(OUTD / "week.json", "w"), indent=1, default=str)
srv.terminate()
