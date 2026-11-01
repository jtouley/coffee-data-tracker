"""One day's job of Design A (the weekly omnibus crawl), against the mock, with time compressed.

usage: weekjob.py SRC PROFILES PORT SCALE LAKE_DIR TODAY LABEL OUT.json [resume-only]
Emulates cmd_weekly's ingest steps with the worktree's own functions: ingest.open_run (continue an unfinished run
of the last 6 days), select_states, ingest.run with the 170-min deadline, then the --retry-failed pass. Probe,
enrich, publish (stubbed) and the dbt build are not simulated. Progress (roasters done) goes to OUT.progress so the
driver can SIGKILL the job at 50%.
"""
import functools, json, os, sys, threading, time

SRC, PROFILES, PORT, S, LAKE, TODAY, LABEL, OUT = sys.argv[1:9]
RESUME_ONLY = len(sys.argv) > 9 and sys.argv[9] == "resume-only"
PORT, S = int(PORT), float(S)
sys.path.insert(0, SRC)
import httpx  # noqa: E402

from coffeedata import http, ingest, lineage, ratelimit, registry  # noqa: E402
from coffeedata.lake import Lake  # noqa: E402
from coffeedata.models import Roaster  # noqa: E402

assert ingest.__file__.startswith(os.path.abspath(SRC)), ingest.__file__
_mono, _sleep = time.monotonic, time.sleep


class FastTime:
    def __getattr__(self, k):
        return getattr(time, k)

    @staticmethod
    def monotonic():
        return _mono() * S

    @staticmethod
    def sleep(x):
        _sleep(max(0.0, x) / S)


http.time = FastTime()
ingest.time = FastTime()
ingest.PlatformLimiter = functools.partial(ratelimit.PlatformLimiter, clock=FastTime.monotonic, sleep=FastTime.sleep)
lineage.code_version = lambda: LABEL
Lake.publish = lambda self, msg, **k: 0


class ToLocalhost(httpx.BaseTransport):
    def __init__(self):
        self.t = httpx.HTTPTransport(limits=httpx.Limits(max_connections=256, max_keepalive_connections=64))

    def handle_request(self, request):
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=PORT)
        r = self.t.handle_request(httpx.Request(request.method, url, headers=request.headers))
        return httpx.Response(r.status_code, headers=r.headers, stream=r.stream, extensions=r.extensions)


ingest.PoliteClient = functools.partial(http.PoliteClient, transport=ToLocalhost())
prof = json.load(open(PROFILES))
states = [registry.RoasterState(Roaster(roaster_id=p["roaster_id"], name=p["name"], host=p["host"],
                                        platform=p["platform"]), p["status"], None) for p in prof]
registry.current = lambda lake, *a, **k: states
if os.environ.get("KNOWN") == "profile" and hasattr(ingest, "known_products"):
    # A lake with last week's history: each store's product count at its latest ok fetch (the mock's own count).
    ingest.known_products = lambda lake: {p["roaster_id"]: p["products"] for p in prof if p["products"]}

done = [0]
_one = ingest.ingest_one


def counted(client, roaster, *a):
    r = _one(client, roaster, *a)
    done[0] += 1
    return r


ingest.ingest_one = counted


def progress(total):
    while True:
        with open(OUT + ".progress", "w") as f:
            f.write(f"{done[0]} {total}\n")
        _sleep(0.5)


lake = Lake(url=LAKE)
lake.ensure_schemas()
T0 = FastTime.monotonic()
open_run = ingest.open_run(lake, TODAY)
res = {"label": LABEL, "today": TODAY, "open_run": open_run}
deadline = T0 + 170 * 60
out = {"complete": True}
if RESUME_ONLY and not open_run:
    res |= {"skipped": "no unfinished run to resume"}
else:
    run_date = open_run or TODAY
    todo, mode = ingest.select_states(lake, run_date)
    threading.Thread(target=progress, args=(len(todo),), daemon=True).start()
    out = ingest.run(lake, todo, run_date, workers=32, mode=mode, deadline=deadline)
    res |= {"run_date": run_date, "todo": len(todo), "main": out, "main_wall_min": (FastTime.monotonic() - T0) / 60}
    if out["complete"]:
        retry, _ = ingest.select_states(lake, run_date, retry_failed=True)
        res["retry"] = ingest.run(lake, retry, run_date, workers=16, mode="retry_failed", deadline=deadline)
        res["retry_todo"] = len(retry)
if RESUME_ONLY and hasattr(ingest, "select_parked"):  # Tuesday: `coffeedata crawl-retry` for parked stores
    pdir = lake.root / "parked"
    runs = sorted(p.name.split("=", 1)[1] for p in pdir.glob("run_date=*") if any(p.iterdir())) if pdir.exists() else []
    if runs:
        st, resume = ingest.select_parked(lake, runs[-1])
        res["crawl_retry"] = ingest.run(lake, st, runs[-1], workers=8, mode="retry_failed", resume=resume)
        res["crawl_retry_todo"] = len(st)
res["wall_min"] = (FastTime.monotonic() - T0) / 60
res["server"] = httpx.get(f"http://127.0.0.1:{PORT}/__stats").json()
for k in ("main", "retry", "crawl_retry"):
    if k in res:
        res[k].pop("failures", None)
json.dump(res, open(OUT, "w"), default=str)
print(LABEL, "wall_min", round(res["wall_min"], 1), "complete", out["complete"])
