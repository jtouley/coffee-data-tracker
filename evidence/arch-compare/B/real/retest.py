"""Targeted real retest of Design A's unsuccessful Shopify stores with the resumable-pagination fix. Publishing off.

ingest.run (PR #62 per-store backoff + deferred rounds, plus PAGINATION.patch) on the 33 stores in retest_list.txt,
local lake real/retest_lake. Every response goes to B/retest_responses.jsonl (page-1 product ids recorded).
"""
import json, os, sys, threading, time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

B = Path("/home/user/coffee-data-tracker/build/arch-compare/B")
WT = B / "wt"
LAKE = B / "real" / "retest_lake"
LOG = B / "retest_responses.jsonl"

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
lock = threading.Lock()
orig_send = PoliteClient._send


def logged_send(self, url, params=None):
    u = urlparse(url)
    rec = {"host": u.hostname, "path": u.path, "page": (params or {}).get("page")}
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
            prods = r.json().get("products") or []
            rec["products"] = len(prods)
            if str((params or {}).get("page")) == "1":
                rec["ids"] = [str(p.get("id")) for p in prods]
        except Exception:  # noqa: BLE001
            pass
    with lock:
        log_f.write(json.dumps(rec) + "\n")
        log_f.flush()
    return r


PoliteClient._send = logged_send
from coffeedata import registry  # noqa: E402

want = [l.strip() for l in open(B / "real" / "retest_list.txt") if l.strip()]
states = {s.roaster.roaster_id: s for s in registry.current(lake)}
todo = [states[r] for r in want]
assert len(todo) == len(want) == 33, (len(todo), len(want))
t0 = time.time()
out = ingest.run(lake, todo, datetime.now(UTC).date().isoformat(), mode="only")
out["wall_s"] = round(time.time() - t0, 1)
json.dump(out, open(B / "real" / "retest_out.json", "w"), indent=1, default=str)
print(json.dumps(out, default=str)[:3000], flush=True)
