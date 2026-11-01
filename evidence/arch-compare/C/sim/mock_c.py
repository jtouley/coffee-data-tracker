"""The shared localhost mock (build/crawl-plan/sim/mockserver.py), with SCENARIO.md's Shopify behaviour and faults.

usage: mock_c.py PROFILES PORT SCENARIO      (SCENARIO: S1 | S2)

Reuses mockserver.py's stores, payloads and latencies unchanged; replaces only:
- its Shopify bucket: 6% of Shopify stores (sha256(host) chosen) answer 429 + Retry-After 60 to every request;
  S2 adds an IP-wide limit: above 120 Shopify requests in the last 60 s (2 req/s sustained) any store answers 429;
- day 3 (sim seconds 2*86400+3600 to +7200, i.e. 01:00-03:00): every WooCommerce host answers 503;
- its clock: GET /__clock?t=<sim seconds since week start>&s=<scale> sets the simulated time and the scale, so the
  runner can jump between scheduled jobs. Latencies follow the current scale.
"""

import collections
import hashlib
import json
import sys
import time

PROFILES, PORT, SCEN = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sys.argv = ["mockserver.py", PROFILES, str(PORT), "10", "normal"]
sys.path.insert(0, "/home/user/coffee-data-tracker/build/crawl-plan/sim")
import mockserver as m  # noqa: E402

CLOCK = {"t": 0.0, "mono": time.monotonic(), "s": 10.0}
m.now = lambda: CLOCK["t"] + (time.monotonic() - CLOCK["mono"]) * CLOCK["s"]
m.shopify_gate = lambda: 0  # the scenario's limits below replace mockserver's IP bucket
REFUSE_SHARE = 0.06
WOO_OUT = (2 * 86400 + 3600, 2 * 86400 + 3 * 3600)
IP_WINDOW, IP_MAX = 60.0, 120
recent: collections.deque = collections.deque()
stats = m.counts
stats.update({"by_adapter": {}, "429_refuser": 0, "429_ip": 0, "503_woo": 0})


def refuser(host: str) -> bool:
    return int(hashlib.sha256(f"refuse:{host}".encode()).hexdigest(), 16) % 10000 < REFUSE_SHARE * 10000


_route = m.route


def route(host, path, q):
    if path == "/__clock":
        CLOCK.update(t=float(q["t"]), s=float(q["s"]), mono=time.monotonic())
        m.S = CLOCK["s"]
        return 200, "application/json", json.dumps({"t": CLOCK["t"]}), "robots", None
    if host == "cdn5.editmysite.com":
        a = "square_online"
    else:
        a = m.BY_HOST[host][1]["adapter"]
    b = stats["by_adapter"].setdefault(a, {"req": 0, "429": 0, "503": 0})
    b["req"] += 1
    t = m.now()
    if a == "shopify":
        recent.append(t)
        while recent and recent[0] < t - IP_WINDOW:
            recent.popleft()
        if refuser(host):
            stats["429_refuser"] += 1
            b["429"] += 1
            return 429, "text/plain", "Too Many Requests", "robots", 60
        if SCEN == "S2" and len(recent) > IP_MAX:
            stats["429_ip"] += 1
            b["429"] += 1
            return 429, "text/plain", "Too Many Requests", "robots", 60
    if a == "woocommerce" and WOO_OUT[0] <= t < WOO_OUT[1]:
        stats["503_woo"] += 1
        b["503"] += 1
        return 503, "text/plain", "Service Unavailable", "robots", None
    return _route(host, path, q)


m.route = route

if __name__ == "__main__":
    print("refusers", sum(refuser(p["host"].lower()) for p in m.PROF if p["adapter"] == "shopify"), flush=True)
    m.uvicorn.run(m.app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False, backlog=2048)
