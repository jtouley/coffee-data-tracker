"""Run Design A's simulated week for one scenario and one fault placement.

usage: runweek.py SCENARIO(s1|s2) PLACEMENT(calendar|drill) PORT [SCALE]
calendar: faults on the SCENARIO's calendar days. A's jobs run Mon (day 1, weekly) and Tue (day 2, resume-only
          catch-up), so the day-3 WooCommerce outage and the day-5 kill find no job running.
drill:    the same faults moved onto A's own run: WooCommerce 503 for the first 2 h of Monday's job, and Monday's
          job SIGKILLed at 50% progress; Tuesday's catch-up resumes.
"""
import json, os, signal, subprocess, sys, tempfile, time

SC, PL, PORT = sys.argv[1], sys.argv[2], int(sys.argv[3])
S = sys.argv[4] if len(sys.argv) > 4 else "10"
HERE = os.path.dirname(os.path.abspath(__file__))
PY = "/home/user/coffee-data-tracker/.venv/bin/python"
SRC = os.path.abspath(os.path.join(HERE, "..", "wt", "src"))
PROF = os.environ.get("PROF") or os.path.join(HERE, "profiles.json")
OUTD = os.path.join(HERE, os.environ.get("RUNS", "runs"), f"{SC}_{PL}")
os.makedirs(OUTD, exist_ok=True)
LAKE = tempfile.mkdtemp(prefix=f"A-{SC}-{PL}-lake-")
DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]
jobs = []
for i, day in enumerate(DAYS[:2]):  # A schedules only Mon (weekly) and Tue (catch-up); days 3-7 have no job
    label = "mon_weekly" if i == 0 else "tue_catchup"
    port = PORT + i  # a fresh port: the killed job's sockets can keep the old one busy
    woo = ["0", "7200"] if (PL == "drill" and i == 0) else []
    srv = subprocess.Popen([PY, os.path.join(HERE, "mockweek.py"), PROF, str(port), S, SC, *woo],
                           stdout=open(f"{OUTD}/srv_{label}.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(120):  # wait until the mock answers: a robots.txt failure is cached for 15 min
        if subprocess.run(["curl", "-sf", f"http://127.0.0.1:{port}/__stats"], capture_output=True).returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise SystemExit("mock did not start")
    out = f"{OUTD}/{label}.json"
    args = [PY, os.path.join(HERE, "weekjob.py"), SRC, PROF, str(port), S, LAKE, day, label, out]
    if i:
        args.append("resume-only")
    job = subprocess.Popen(args, stdout=open(f"{OUTD}/{label}.log", "w"), stderr=subprocess.STDOUT,
                           cwd=tempfile.gettempdir())
    t0, killed = time.monotonic(), None
    while job.poll() is None:
        time.sleep(0.5)
        if PL == "drill" and i == 0 and os.path.exists(out + ".progress"):
            try:
                d, n = map(int, open(out + ".progress").read().split())
            except ValueError:
                continue
            if n and d >= n / 2:
                job.send_signal(signal.SIGKILL)
                killed = {"done": d, "of": n, "sim_min": (time.monotonic() - t0) * float(S) / 60}
    job.wait()
    stats = json.loads(subprocess.run(["curl", "-s", f"http://127.0.0.1:{port}/__stats"], capture_output=True,
                                      text=True).stdout or "{}")
    srv.terminate(); srv.wait()
    rec = json.load(open(out)) if os.path.exists(out) and not killed else {"label": label, "today": day}
    rec |= {"killed": killed, "server": stats, "exit": job.returncode}
    if killed:
        rec["wall_min"] = killed["sim_min"]
    json.dump(rec, open(out, "w"), default=str)
    jobs.append(rec)
    print(label, {k: rec.get(k) for k in ("wall_min", "killed", "exit")}, flush=True)
json.dump({"scenario": SC, "placement": PL, "scale": float(S), "lake": LAKE, "jobs": jobs},
          open(f"{OUTD}/week.json", "w"), default=str)
print("LAKE", LAKE)
