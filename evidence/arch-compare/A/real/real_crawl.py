"""Design A as a real crawl of every ingestable Shopify roaster in the registry seed. PUBLISHING OFF: local lake only.

Runs the worktree's own code the way `weekly` would for the crawl: ingest.run (per-store backoff, deferred rounds,
refused labelling) with the 170-min fetch deadline, then the --retry-failed pass inside the same deadline.
Normal politeness (robots.txt, the crawler's User-Agent, per-host spacing) is the client's own and is untouched.
Every response (or network error) is logged to A/real_responses.jsonl. A hard cap stops the crawl at MAX_REQUESTS.
"""
import json, os, sys, threading, time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

WT = Path("/home/user/coffee-data-tracker/build/arch-compare/A/wt")
OUT = Path("/home/user/coffee-data-tracker/build/arch-compare/A")
LAKE = OUT / "real" / "lake"
MAX_REQUESTS = 9000  # ~1,070 stores x ~3.3 requests, plus retries; a guard, not a target
RUN_DATE = "2026-10-31"

# ---- publishing off: verify before anything is imported or fetched
for k in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
    assert not os.environ.get(k), f"{k} is set"
os.environ["COFFEEDATA_LAKE"] = str(LAKE)
os.chdir(WT)
sys.path.insert(0, str(WT / "src"))
import huggingface_hub  # noqa: E402


class _NoHF:
    def __init__(self, *a, **k):
        raise RuntimeError("HF access blocked: publishing is off")


def _no_hf(*a, **k):
    raise RuntimeError("HF access blocked: publishing is off")


huggingface_hub.HfApi = _NoHF
for name in ("upload_folder", "upload_file", "create_commit", "hf_hub_download", "snapshot_download", "list_repo_tree"):
    if hasattr(huggingface_hub, name):
        setattr(huggingface_hub, name, _no_hf)
import huggingface_hub.hf_api as _hfa  # noqa: E402

_hfa.HfApi = _NoHF
from coffeedata import ingest, registry  # noqa: E402
from coffeedata.http import FetchError, PoliteClient  # noqa: E402
from coffeedata.lake import Lake  # noqa: E402

lake = Lake()
assert not lake.remote and lake.url == str(LAKE), lake.url
try:
    huggingface_hub.HfApi()
    raise SystemExit("HF stub not active")
except RuntimeError:
    pass
Lake.publish = lambda self, msg, **k: 0  # a local lake publishes nothing anyway; make it explicit
print("checks: tokens unset, HfApi stubbed, lake local:", lake.url, flush=True)
lake.ensure_schemas()

log_f = open(OUT / "real_responses.jsonl", "a")
lock = threading.Lock()
n = {"req": 0}


class BudgetExhausted(FetchError):
    pass


orig_send = PoliteClient._send


def logged_send(self, url, params=None):
    with lock:
        if n["req"] >= MAX_REQUESTS:
            raise BudgetExhausted("request cap reached")
        n["req"] += 1
    u = urlparse(url)
    rec = {"host": u.hostname, "path": u.path, "page": (params or {}).get("page")}
    t0 = time.time()
    try:
        r = orig_send(self, url, params)
    except Exception as e:
        rec |= {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "status": None,
                "retry_after": None, "error": type(e).__name__, "elapsed_s": round(time.time() - t0, 3)}
        with lock:
            log_f.write(json.dumps(rec) + "\n")
            log_f.flush()
        raise
    rec |= {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "status": r.status_code,
            "retry_after": r.headers.get("retry-after"), "elapsed_s": round(time.time() - t0, 3)}
    with lock:
        log_f.write(json.dumps(rec) + "\n")
        log_f.flush()
    return r


PoliteClient._send = logged_send

states = [s for s in registry.current(lake) if s.roaster.platform == "shopify" and s.status in ingest.INGESTABLE]
print("shopify roasters:", len(states), flush=True)
t0 = time.monotonic()
deadline = t0 + 170 * 60
res = {"started": datetime.now(UTC).isoformat(timespec="seconds"), "roasters": len(states)}
main = ingest.run(lake, states, RUN_DATE, workers=32, mode="full", deadline=deadline)
res["main"] = main
res["main_wall_min"] = round((time.monotonic() - t0) / 60, 2)
print("main pass", res["main_wall_min"], "min", json.dumps(main["shopify_limiter"]), main["deferred"], flush=True)
if main["complete"]:
    retry, _ = ingest.select_states(lake, RUN_DATE, retry_failed=True)
    retry = [s for s in retry if s.roaster.platform == "shopify"]
    res["retry_todo"] = len(retry)
    if retry:
        res["retry"] = ingest.run(lake, retry, RUN_DATE, workers=16, mode="retry_failed", deadline=deadline)
res["wall_min"] = round((time.monotonic() - t0) / 60, 2)
res["requests_logged"] = n["req"]
res["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
json.dump(res, open(OUT / "real" / "summary.json", "w"), indent=1, default=str)
print("done", res["wall_min"], "min,", n["req"], "requests", flush=True)
