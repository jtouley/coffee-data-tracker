"""The live 429 experiment (build/crawl-plan/live.md): are Shopify's 429s per store or per client IP?

200 Shopify storefronts on distinct hosts, picked by sha256("<salt>:" + roaster_id), are fetched in two groups of
100: group 1 at 1 request/s (phase A), group 2 at 3 requests/s (phase B). Then the group-1 stores that got a 429 are
asked again at 1 request/s (phase C). At most 700 requests in all. robots.txt, the User-Agent and per-host spacing are
the crawler's own, unchanged.

Publishing is off, and this refuses to start otherwise: the lake must be a local folder, no Hugging Face token may be
set, and huggingface_hub's upload and download entry points are replaced with ones that raise. It prints aggregates
only (counts, shares, Wilson 95% intervals), never a host, a roaster or a product, because workflow logs on a public
repository are public. The per-response log stays in the local lake folder and goes away with it.

    python -m coffeedata.livetest --lake "$RUNNER_TEMP/lake" [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import resource
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SALT = (
    "live429-2026-10-31"  # the 2026-10-31 run's salt, so a run picks the same 200 stores (live_groups.json)
)
STORES = 200
TOTAL_BUDGET = 700
PHASE_CAP = {"A": 330, "B": 330, "C": 700}
HF_TOKENS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% interval for k of n, in percent."""
    if n == 0:
        return (0.0, 100.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(100 * max(0.0, c - h), 1), round(100 * min(1.0, c + h), 1))


def share(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n if n else 0:.1f}% [{lo}, {hi}]"


def publishing_off(lake_dir: str | Path) -> None:
    """Make every publish path fail loudly, before anything from coffeedata is imported."""
    for k in HF_TOKENS:
        if os.environ.get(k):
            raise SystemExit(f"{k} is set: the live test runs with publishing off and no Hugging Face token")
    if "://" in str(lake_dir) or str(lake_dir).startswith(("hf:", "s3:", "gs:")):
        raise SystemExit("--lake must be a local folder")
    os.environ["COFFEEDATA_LAKE"] = str(lake_dir)
    import huggingface_hub

    def _blocked(*a: object, **k: object) -> Any:
        raise RuntimeError("Hugging Face access is blocked in the live test")

    for name in (
        "HfApi",
        "hf_hub_download",
        "snapshot_download",
        "upload_file",
        "upload_folder",
        "create_commit",
    ):
        if hasattr(huggingface_hub, name):
            setattr(huggingface_hub, name, _blocked)


def pick(states: list, registry: Any) -> list:
    """The first STORES storefronts by salted hash, one roaster per normalized host."""

    def key(s: Any) -> str:
        return hashlib.sha256(f"{SALT}:{s.roaster.roaster_id}".encode()).hexdigest()

    seen: set[str] = set()
    out = []
    for s in sorted(states, key=key):
        host = registry.normalize_domain(s.roaster.host)
        if host not in seen:
            seen.add(host)
            out.append(s)
    return out[:STORES]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lake", required=True, help="a local, empty or throwaway lake folder")
    ap.add_argument("--dry-run", action="store_true", help="select the stores and print counts; no requests")
    args = ap.parse_args(argv)
    publishing_off(args.lake)
    lake_dir = Path(args.lake)
    logging.disable(logging.CRITICAL)  # the crawler's own log lines name roasters and hosts: keep them out

    from . import ingest, registry
    from .http import USER_AGENT, FetchError, PoliteClient, user_agent
    from .lake import Lake

    lake = Lake()
    assert not lake.remote, lake.url
    states = [
        s for s in registry.current(lake) if s.roaster.platform == "shopify" and s.status in ingest.INGESTABLE
    ]
    chosen = pick(states, registry)
    g1, g2 = chosen[:100], chosen[100:]
    print(f"eligible shopify roasters: {len(states)}; picked {len(chosen)} on distinct hosts (salt {SALT})")
    print(f"user agent has a contact: {'yes' if user_agent() != USER_AGENT else 'no'}")
    if args.dry_run or len(chosen) < STORES:
        return 0 if args.dry_run else 1

    lock = threading.Lock()
    state: dict[str, Any] = {"phase": "A", "phase_n": 0, "total": 0, "cut": 0}
    responses: list[dict] = []
    log_path = lake_dir / "live_responses.jsonl"  # local only; never printed, never uploaded

    class BudgetExhausted(FetchError):
        pass

    orig_send = PoliteClient._send

    def logged_send(self: PoliteClient, url: str, params: dict | None = None) -> Any:
        with lock:
            if state["total"] >= TOTAL_BUDGET or state["phase_n"] >= PHASE_CAP[state["phase"]]:
                state["cut"] += 1
                raise BudgetExhausted("request budget exhausted")
            state["total"] += 1
            state["phase_n"] += 1
        rec: dict[str, Any] = {"phase": state["phase"], "host": urlparse(url).hostname, "t": time.time()}
        try:
            r = orig_send(self, url, params)
        except Exception as e:
            rec |= {"status": None, "error": type(e).__name__}
            raise
        else:
            rec |= {"status": r.status_code, "retry_after": r.headers.get("retry-after")}
            return r
        finally:
            with lock:
                responses.append(rec)

    PoliteClient._send = logged_send  # type: ignore[method-assign]

    def phase(name: str, todo: list, rps: float) -> None:
        state.update(phase=name, phase_n=0)
        t = time.time()
        s = ingest.run(lake, todo, datetime.now(UTC).date().isoformat(), shopify_rps=rps, mode="sample")
        rs = [r for r in responses if r["phase"] == name]
        hosts = {registry.normalize_domain(x.roaster.host) for x in todo}
        hit = {registry.normalize_domain(r["host"]) for r in rs if r["status"] == 429}
        a = s["by_adapter"].get("shopify", {})
        n429 = sum(r["status"] == 429 for r in rs)
        n430 = sum(r["status"] == 430 for r in rs)
        ok = a.get("roasters", 0) - a.get("errors", 0)
        ra = Counter(r["retry_after"] for r in rs if r["status"] == 429)
        print(
            f"phase {name}: {len(todo)} stores at {rps} req/s, wall {time.time() - t:.0f} s\n"
            f"  requests {len(rs)}; 429s {share(n429, len(rs))}; 430s {n430}\n"
            f"  stores with a 429 {share(len(hit & hosts), len(hosts))}; stores ok {share(ok, a.get('roasters', 0))}\n"
            f"  limiter {json.dumps(s['shopify_limiter'], sort_keys=True)}; Retry-After values {dict(ra)}"
        )

    phase("A", g1, 1.0)
    phase("B", g2, 3.0)
    hit_a = {
        registry.normalize_domain(r["host"]) for r in responses if r["phase"] == "A" and r["status"] == 429
    }
    c = [s for s in g1 if registry.normalize_domain(s.roaster.host) in hit_a][:20]
    left = TOTAL_BUDGET - state["total"]
    if c and left >= 3 * len(c):
        phase("C", c, 1.0)
    else:
        print(f"phase C skipped: {len(c)} stores, {left} requests left")
    with log_path.open("w") as f:
        for r in responses:
            f.write(json.dumps(r) + "\n")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(f"total requests {state['total']} (cap {TOTAL_BUDGET}); requests refused by the cap {state['cut']}")
    print(f"peak RSS {peak} MB; finished {datetime.now(UTC).isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
