"""One scheduled job of Design C, run as its own process (as a Cloud Run job task would be), against the mock.

usage: job.py STEP CONNECTOR RUN_DATE PORT SIM_T0 SCALE OUT.json PROGRESS.json
Env: COFFEEDATA_LAKE, COFFEEDATA_RAW. Every clock the crawl reads (http, ingest, the limiters, pipelines) runs on
simulated time: SIM_T0 seconds after 2026-11-02T00:00Z at job start, SCALE times faster than real time.
"""

import functools
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

STEP, CONN, RUN_DATE, PORT, T0, S, OUT, PROG = sys.argv[1:9]
PORT, T0, S = int(PORT), float(T0), float(S)
EPOCH = datetime(2026, 11, 2, tzinfo=UTC)
_mono, _sleep = time.monotonic, time.sleep
M0 = _mono()


SKIP = [0.0]  # simulated seconds skipped while the sweep idles between waves


def sim_s() -> float:
    return T0 + SKIP[0] + (_mono() - M0) * S


class FastTime:
    def __getattr__(self, k):
        return getattr(time, k)

    @staticmethod
    def monotonic():
        return sim_s()

    @staticmethod
    def sleep(x):
        _sleep(max(0.0, x) / S)


class SimDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return EPOCH + timedelta(seconds=sim_s())


import httpx  # noqa: E402

from coffeedata import http, ingest, lineage, pipelines, ratelimit  # noqa: E402
from coffeedata.lake import Lake  # noqa: E402

class SkipTime(FastTime):
    """pipelines' own clock: the sweep's waits between waves (no request in flight) jump the clock, the mock's too."""

    @staticmethod
    def sleep(x):
        if x > 0:
            SKIP[0] += x
            httpx.get(f"http://127.0.0.1:{PORT}/__clock", params={"t": sim_s(), "s": S})


http.time = ingest.time = FastTime()
pipelines.time = SkipTime()
ingest.datetime = SimDatetime
pipelines.utcnow = lambda: SimDatetime.now().replace(microsecond=0)
lineage.code_version = lambda: "design-c"
from coffeedata import extract  # noqa: E402

extract.llm_enabled = lambda: False  # the simulation never calls Claude (the rules still run)
ingest.PlatformLimiter = functools.partial(ratelimit.PlatformLimiter, clock=FastTime.monotonic, sleep=FastTime.sleep)


class ToLocalhost(httpx.BaseTransport):
    def __init__(self):
        self.t = httpx.HTTPTransport(limits=httpx.Limits(max_connections=256, max_keepalive_connections=64))

    def handle_request(self, request):
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=PORT)
        r = self.t.handle_request(httpx.Request(request.method, url, headers=request.headers))
        return httpx.Response(r.status_code, headers=r.headers, stream=r.stream, extensions=r.extensions,
                              request=request)


ingest.PoliteClient = functools.partial(http.PoliteClient, transport=ToLocalhost())

progress = {"total": 0, "done": 0}
_run, _one = ingest.run, ingest.ingest_one


def run(lake, todo, *a, **kw):
    progress["total"] += len(todo)
    json.dump(progress, open(PROG, "w"))
    return _run(lake, todo, *a, **kw)


LOCK = threading.Lock()


def one(client, roaster, resume=None, known_products=None):
    r = _one(client, roaster, resume, known_products)
    with LOCK:
        progress["done"] += 1
        json.dump(progress, open(PROG + ".tmp", "w"))
        os.replace(PROG + ".tmp", PROG)
    return r


ingest.run, ingest.ingest_one = run, one
pipelines.ingest.run = run
lake = Lake()
cfg = pipelines.load_config()
t_start = sim_s()
if STEP == "connector":
    out = pipelines.run_connector(lake, CONN, RUN_DATE, cfg)
elif STEP == "sweep":
    out = pipelines.run_sweep(lake, RUN_DATE, cfg)
elif STEP == "html":
    out = pipelines.run_html_fallback(lake, RUN_DATE, cfg)
elif STEP == "enrich":
    out = pipelines.run_enrich(lake, RUN_DATE, cfg)
else:
    out = pipelines.run_merge(lake, RUN_DATE, cfg)
out["sim_start_s"], out["sim_end_s"] = t_start, sim_s()
json.dump(out, open(OUT, "w"), default=str)
os._exit(0)
