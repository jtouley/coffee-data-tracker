"""One Design C job as a REAL crawl of the registry's Shopify roasters. Publishing off; local lake and bucket only.

usage: real_job.py STEP RUN_DATE [SHARD] [--check]     STEP: shard | sweep | html
The job is the prototype's own pipelines.run_connector / run_sweep / run_html_fallback (PR #62 per-store backoff
and deferred rounds, 1.5 req/s Shopify budget, normal PoliteClient politeness). Every response is appended to
C/real_responses.jsonl, tagged with the job.
"""
import json, os, sys, threading, time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

C = Path("/home/user/coffee-data-tracker/build/arch-compare/C")
WT = C / "wt"
LAKE, BUCKET, LOG = C / "real" / "lake", C / "real" / "bucket", C / "real_responses.jsonl"
STEP, RUN_DATE = sys.argv[1], sys.argv[2]
SHARD = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None
TAG = f"shard{SHARD}" if STEP == "shard" else STEP

# ---- publishing off, verified before anything is imported or fetched
for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
    os.environ.pop(k, None)
    assert k not in os.environ
os.environ["COFFEEDATA_LAKE"] = str(LAKE)
os.environ["COFFEEDATA_RAW"] = str(BUCKET)
os.chdir(WT)  # the registry seed is config/roasters.csv; config/connectors.toml
sys.path.insert(0, str(WT / "src"))
import huggingface_hub  # noqa: E402


def _no_hf(*a, **k):
    raise RuntimeError("HF access blocked: publishing is off for the real crawl")


for name in ("HfApi", "hf_hub_download", "upload_folder", "create_commit", "snapshot_download", "HfFileSystem"):
    setattr(huggingface_hub, name, _no_hf)
import huggingface_hub.hf_api  # noqa: E402

huggingface_hub.hf_api.HfApi = _no_hf
import coffeedata.lake as clake  # noqa: E402
from coffeedata import pipelines  # noqa: E402
from coffeedata.http import PoliteClient  # noqa: E402

lake = clake.Lake()
assert not lake.remote and lake.url == str(LAKE), lake.url
try:
    huggingface_hub.HfApi()
    raise SystemExit("HF stub not in place")
except RuntimeError:
    pass
print("lake:", lake.url, "remote:", lake.remote, "bucket:", BUCKET,
      "HF tokens:", [k for k in os.environ if "HF_TOKEN" in k or "HUGGING" in k], flush=True)
if "--check" in sys.argv:
    states = [s for s in pipelines.roster(lake, RUN_DATE) if s.roaster.adapter == "shopify"]
    from collections import Counter
    print("shopify roasters:", len(states), "per shard:",
          sorted(Counter(pipelines.shard_of(s.roaster.roaster_id) for s in states).items()))
    raise SystemExit("check only: publishing-off verified")

log_f = open(LOG, "a")
lock = threading.Lock()
orig_send = PoliteClient._send


def logged_send(self, url, params=None):
    u = urlparse(url)
    rec = {"job": TAG, "host": u.hostname, "path": u.path, "page": (params or {}).get("page")}
    t0 = time.time()
    try:
        r = orig_send(self, url, params)
    except Exception as e:
        rec |= {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "status": None, "retry_after": None,
                "error": type(e).__name__, "elapsed_s": round(time.time() - t0, 3)}
        with lock:
            log_f.write(json.dumps(rec) + "\n")
            log_f.flush()
        raise
    lim = self._limiter
    rec |= {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "status": r.status_code,
            "retry_after": r.headers.get("retry-after"), "elapsed_s": round(time.time() - t0, 3),
            "rate_effective": round(1 / lim.interval, 3) if lim else None}
    with lock:
        log_f.write(json.dumps(rec) + "\n")
        log_f.flush()
    return r


PoliteClient._send = logged_send
cfg = pipelines.load_config()
t0 = time.time()
started = datetime.now(UTC).isoformat(timespec="seconds")
if STEP == "shard":
    out = pipelines.run_connector(lake, "shopify", RUN_DATE, cfg, shard_k=SHARD)
elif STEP == "sweep":
    out = pipelines.run_sweep(lake, RUN_DATE, cfg)
else:
    out = pipelines.run_html_fallback(lake, RUN_DATE, cfg)
out |= {"tag": TAG, "started": started, "wall_s": round(time.time() - t0, 1)}
(C / "real" / "jobs").mkdir(exist_ok=True)
json.dump(out, open(C / "real" / "jobs" / f"{TAG}.json", "w"), indent=1, default=str)
print(json.dumps(out, default=str)[:2000], flush=True)
