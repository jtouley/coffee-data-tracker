"""Run one `coffeedata` CLI command (crawl or merge) against the localhost mock, on the scaled sim clock.

usage: jobshim.py <coffeedata args...>
env: SIM_SRC (src dir), PORT, SCALE, SIM_T0 (sim epoch seconds when the job starts), PROFILES, COFFEEDATA_LAKE,
     KILL_AT (optional: SIGKILL this process once that share of the job's roasters is done)
The registry is the profile file (registry.current is replaced); nothing leaves 127.0.0.1.
"""
import functools, json, os, signal, sys, threading, time
from datetime import datetime

SRC, PORT, S, T0 = os.environ["SIM_SRC"], int(os.environ["PORT"]), float(os.environ["SCALE"]), float(os.environ["SIM_T0"])
KILL_AT = float(os.environ.get("KILL_AT") or 0)
sys.path.insert(0, SRC)
import httpx  # noqa: E402

from coffeedata import cli, http, ingest, lineage, ratelimit, registry  # noqa: E402
from coffeedata.models import Roaster  # noqa: E402

assert ingest.__file__.startswith(os.path.abspath(SRC))
_mono, _sleep, M0 = time.monotonic, time.sleep, time.monotonic()


class FastTime:
    def __getattr__(self, k):
        return getattr(time, k)

    @staticmethod
    def monotonic():
        return _mono() * S

    @staticmethod
    def sleep(x):
        _sleep(max(0.0, x) / S)


class SimDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(T0 + (_mono() - M0) * S, tz)


class ToLocalhost(httpx.BaseTransport):
    def __init__(self):
        self.t = httpx.HTTPTransport(limits=httpx.Limits(max_connections=256, max_keepalive_connections=64))

    def handle_request(self, request):
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=PORT)
        r = self.t.handle_request(httpx.Request(request.method, url, headers=request.headers))
        return httpx.Response(r.status_code, headers=r.headers, stream=r.stream, extensions=r.extensions)


http.time = FastTime()
ingest.time = FastTime()
ingest.datetime = SimDateTime
ingest.PlatformLimiter = functools.partial(ratelimit.PlatformLimiter, clock=FastTime.monotonic, sleep=FastTime.sleep)
ingest.PoliteClient = functools.partial(http.PoliteClient, transport=ToLocalhost())
lineage.code_version = lambda: "arch-B-sim"
PROF = json.load(open(os.environ["PROFILES"]))
registry.current = lambda lake, seed=None: [
    registry.RoasterState(Roaster(roaster_id=p["roaster_id"], name=p["name"], host=p["host"], platform=p["platform"]),
                          p["status"], None) for p in PROF]

if KILL_AT:
    total, done, lock = [0], [0], threading.Lock()
    _run, _one = ingest.run, ingest.ingest_one

    def run(lake, todo, *a, **k):
        total[0] = len(todo)
        return _run(lake, todo, *a, **k)

    def one(client, roaster, *a):
        r = _one(client, roaster, *a)
        with lock:
            done[0] += 1
            if total[0] and done[0] >= KILL_AT * total[0]:
                print(f"KILLED at {done[0]}/{total[0]}", file=sys.stderr, flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
        return r

    ingest.run, ingest.ingest_one = run, one

cli.main(sys.argv[1:])
