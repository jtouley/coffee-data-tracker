"""Design B's Shopify connector job as a real crawl: every Shopify roaster in the registry seed. Publishing off.

The job is the prototype's own `connectors.crawl(lake, "shopify", day)` (PR #62 per-store backoff and deferred
retries, its 225-min deadline, PAGINATION.patch + PAGINATION.v2.patch), with a local lake, then the separate retry job
for parked stores (`crawl-retry --connector shopify`). Every response is appended to real_responses.jsonl.
"""
import json, os, sys, threading, time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

B = Path("/home/user/coffee-data-tracker/build/arch-compare/B")
WT = B / "real" / "frozen"  # a frozen copy of B/wt (v2) taken before the ELT change
LAKE = B / "real" / "lake"
LOG = B / "real_responses.jsonl"

# ---- publishing off, verified before anything is imported or fetched
for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
    os.environ.pop(k, None)
    assert k not in os.environ
os.environ["COFFEEDATA_LAKE"] = str(LAKE)
os.chdir(WT)  # the registry seed is config/roasters.csv
sys.path.insert(0, str(WT / "src"))
import huggingface_hub  # noqa: E402


def _no_hf(*a, **k):
    raise RuntimeError("HF access blocked: publishing is off for the real crawl")


for name in ("HfApi", "hf_hub_download", "upload_folder", "create_commit", "snapshot_download", "HfFileSystem"):
    setattr(huggingface_hub, name, _no_hf)
import huggingface_hub.hf_api  # noqa: E402

huggingface_hub.hf_api.HfApi = _no_hf
import coffeedata.lake as clake  # noqa: E402
from coffeedata import connectors, ingest  # noqa: E402
from coffeedata.http import PoliteClient  # noqa: E402

for name in dir(clake):
    if name in ("HfApi", "HfFileSystem", "hf_hub_download"):
        setattr(clake, name, _no_hf)
lake = clake.Lake()
assert not lake.remote and lake.url == str(LAKE), lake.url
try:
    huggingface_hub.HfApi()
    raise SystemExit("HF stub not in place")
except RuntimeError:
    pass
print("lake:", lake.url, "remote:", lake.remote, "HF tokens:", [k for k in os.environ if "HF_TOKEN" in k or "HUGGING" in k],
      flush=True)

if "--check" in sys.argv:
    raise SystemExit("check only: publishing-off verified")
log_f = open(LOG, "a")
PHASE = ["crawl"]
lock = threading.Lock()
orig_send = PoliteClient._send


def logged_send(self, url, params=None):
    u = urlparse(url)
    rec = {"phase": PHASE[0], "host": u.hostname, "path": u.path, "page": (params or {}).get("page")}
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
    if u.path == "/products.json" and r.status_code == 200:
        try:
            rec["products"] = len(r.json().get("products") or [])
        except Exception:  # noqa: BLE001
            pass
    with lock:
        log_f.write(json.dumps(rec) + "\n")
        log_f.flush()
    return r


PoliteClient._send = logged_send
from coffeedata import cli  # noqa: E402

day = datetime.now(UTC).date().isoformat()
t0 = time.time()
out = connectors.crawl(lake, "shopify", day)
out["wall_s"] = round(time.time() - t0, 1)
json.dump(out, open(B / "real" / "crawl_out.json", "w"), indent=1, default=str)
print(json.dumps(out, default=str)[:3000], flush=True)
# the separate later retry job for stores parked after 3 page backoffs
PHASE[0] = "crawl-retry"
week = connectors.week_of(day)
plake = connectors.PartitionLake(lake, "shopify", week)
states, resume = ingest.select_parked(plake, week)
t1 = time.time()
rout = ingest.run(plake, states, week, workers=8, mode="retry_failed", resume=resume) if states else {}
rout |= {"parked_in": len(states), "wall_s": round(time.time() - t1, 1)}
connectors.compact(plake.part)
json.dump(rout, open(B / "real" / "retry_out.json", "w"), indent=1, default=str)
print(json.dumps(rout, default=str)[:2000], flush=True)
