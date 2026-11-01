"""Localhost mock of every roaster storefront, for the arch-compare SCENARIO week (copy of crawl-plan/sim/mockserver.py).

usage: mockweek.py PROFILES PORT SCALE SCENARIO [WOO_OUTAGE_START_S WOO_OUTAGE_END_S]   (SCENARIO: s1 | s2)
s1: 6% of Shopify stores (fixed, by sha256 of roaster_id) refuse: 429 Retry-After 60 on every catalog request
    (robots.txt still 200, as in the live test). Everything else answers normally.
s2: s1 plus an IP-wide bucket: more than 120 Shopify requests in the trailing 60 s -> any Shopify store 429 (RA 60).
WooCommerce outage: every WooCommerce request answers 503 between the two times (sim seconds since server start).
Time is compressed by SCALE: latencies are divided by it, and the server's clock (throttle, Retry-After) runs
SCALE times faster, matching the client harness, so every number below is in simulated seconds.
"""
import asyncio, json, math, random, sys, time
from urllib.parse import parse_qs

import uvicorn

PROF = json.load(open(sys.argv[1]))
PORT, S, MODE = int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
WOO = (float(sys.argv[5]), float(sys.argv[6])) if len(sys.argv) > 6 else (-1.0, -1.0)
import hashlib, collections  # noqa: E401,E402
REFUSE = {p["host"].lower() for p in PROF if p["adapter"] == "shopify"
          and int(hashlib.sha256(("refuse:" + p["roaster_id"]).encode()).hexdigest(), 16) % 10000 < 600}
WINDOW: collections.deque = collections.deque()
T_START = time.monotonic() * S
BY_HOST = {p["host"].lower(): (i, p) for i, p in enumerate(PROF)}
now = lambda: time.monotonic() * S  # noqa: E731

# Shopify limits by client IP across all its stores: one bucket for every Shopify host.
counts = {"shopify_req": 0, "shopify_429": 0, "shopify_429_refuser": 0, "shopify_429_ip": 0, "req": 0,
          "woo_503": 0, "refusers": len(REFUSE)}
BODY = ("<p>" + "Notes of stone fruit, cocoa and honey; washed at 1,900 m by smallholders. " * 18 + "</p>")
NAMES = ["Ethiopia Guji Natural", "Colombia Huila Washed", "House Espresso Blend", "Decaf Mexico",
         "Kenya Nyeri AA", "Logo Mug", "Tote Bag", "Gift Card"]


def shopify_gate(host, path):
    """Retry-After seconds for a 429, or 0."""
    t = now()
    if MODE == "s2":
        while WINDOW and t - WINDOW[0] > 60:
            WINDOW.popleft()
        WINDOW.append(t)  # every request counts toward the IP's traffic, refused or not
        if len(WINDOW) > 120:
            counts["shopify_429_ip"] += 1
            return 60
    if host in REFUSE and path != "/robots.txt":
        counts["shopify_429_refuser"] += 1
        return 60
    return 0


def latency(kind):
    med = {"page": 0.35, "big": 0.6, "html": 0.5, "robots": 0.15}[kind]
    return min(6.0, random.lognormvariate(math.log(med), 0.6)) / S


def shopify_product(rid, i):
    n = NAMES[i % len(NAMES)]
    return {"id": rid * 100000 + i, "handle": f"p{i}", "title": f"{n} {i}", "vendor": "Roaster",
            "product_type": "Coffee" if i % 8 < 5 else "Merch", "tags": ["coffee", "single origin"],
            "published_at": "2026-09-01T10:00:00-04:00", "body_html": BODY,
            "images": [{"src": f"https://cdn.shopify.com/{rid}/{i}.jpg"}],
            "variants": [{"id": rid * 1000000 + i * 10 + k, "title": t, "option1": t, "price": f"{18 + 4 * k}.00",
                          "grams": g, "available": True} for k, (t, g) in enumerate([("12 oz", 340), ("2 lb", 907)])]}


def woo_product(rid, i, host):
    var = i % 2 == 0
    return {"id": rid * 100000 + i, "name": f"{NAMES[i % len(NAMES)]} {i}", "slug": f"p{i}", "type": "variable" if var else "simple",
            "permalink": f"https://{host}/product/p{i}/", "description": BODY, "short_description": "",
            "is_in_stock": True, "tags": [{"name": "coffee"}], "categories": [{"name": "Coffee"}],
            "images": [{"src": f"https://{host}/img/{i}.jpg"}],
            "prices": {"price": "1800", "currency_minor_unit": 2,
                       "price_range": {"min_amount": "1800", "max_amount": "3400"} if var else None},
            "variations": [{"id": rid * 1000000 + i * 10 + k, "attributes": [{"name": "size", "value": s}]}
                           for k, s in enumerate(["12 oz", "2 lb"])] if var else []}


def sq_item(rid, i):
    return {"id": f"{rid}-{i}", "urlId": f"p{i}", "fullUrl": f"/shop/p/p{i}", "title": f"{NAMES[i % len(NAMES)]} {i}",
            "productType": 1, "categories": ["Coffee"], "tags": ["coffee"], "body": BODY,
            "assetUrl": f"https://images.squarespace-cdn.com/{rid}/{i}.jpg",
            "variants": [{"id": f"{rid}-{i}-v", "attributes": {"Size": "12 oz"}, "priceMoney": {"value": "18.00"},
                          "qtyInStock": 5}]}


def square_product(rid, i):
    return {"id": f"{rid}{i}", "site_product_id": f"{rid}-{i}", "name": f"{NAMES[i % len(NAMES)]} 12oz",
            "site_link": f"product/p{i}/{rid}{i}", "product_type": "physical", "short_description": BODY,
            "price": {"regular_low": 18.0, "regular_high": 18.0}, "badges": {"out_of_stock": False},
            "images": {"data": [{"absolute_url": f"https://cdn5.editmysite.com/{rid}/{i}.jpg"}]}}


def jsonld_page(rid, i, host):
    ld = {"@context": "https://schema.org", "@type": "Product", "name": f"{NAMES[i % len(NAMES)]} {i}",
          "sku": f"{rid}-{i}", "description": BODY, "image": f"https://{host}/{i}.jpg",
          "offers": [{"@type": "Offer", "name": "12 oz", "sku": f"{rid}-{i}-a", "price": "18.00",
                      "availability": "https://schema.org/InStock"}]}
    pad = "<div class='nav'>" + "<a href='/x'>menu item</a>" * 2500 + "</div>"  # ~80 KB, like a real theme page
    return f"<html><head><script type=\"application/ld+json\">{json.dumps(ld)}</script></head><body>{pad}</body></html>"


def sitemap(host, paths):
    return "<?xml version='1.0'?><urlset>" + "".join(f"<url><loc>https://{host}{p}</loc></url>" for p in paths) + "</urlset>"


def route(host, path, q):
    """(status, content_type, body, latency_kind, retry_after)"""
    if host == "cdn5.editmysite.com":
        if path == "/robots.txt":
            return 404, "text/plain", "", "robots", None
        parts = path.split("/")  # /app/store/api/v28/editor/users/{user}/sites/{site}/products
        rid = int(parts[7])
        p = PROF[rid]
        page, per = int(q.get("page", 1)), int(q.get("per_page", 60))
        total = max(1, math.ceil(p["products"] / per))
        data = [square_product(rid, i) for i in range((page - 1) * per, min(p["products"], page * per))]
        return 200, "application/json", json.dumps({"data": data, "meta": {"pagination": {"total_pages": total}}}), "page", None
    rid, p = BY_HOST[host]
    a, n = p["adapter"], p["products"]
    if a == "shopify":
        counts["shopify_req"] += 1
        wait = shopify_gate(host, path)
        if wait:
            counts["shopify_429"] += 1
            return 429, "text/plain", "Too Many Requests", "robots", wait
    if a == "woocommerce" and WOO[0] <= now() - T_START < WOO[1]:
        counts["woo_503"] += 1
        return 503, "text/plain", "Service Unavailable", "robots", None
    if path == "/robots.txt":
        return 200, "text/plain", "User-agent: *\nAllow: /\n", "robots", None
    if a == "shopify":
        if path != "/products.json" or n == 0:
            return 404, "text/html", "not found", "page", None
        page, lim = int(q.get("page", 1)), int(q.get("limit", 250))
        data = [shopify_product(rid, i) for i in range((page - 1) * lim, min(n, page * lim))]
        return 200, "application/json", json.dumps({"products": data}), "big" if data else "page", None
    if a == "woocommerce":
        base = "/wp-json/wc/store/v1/products"
        if n == 0 or not path.startswith(base):
            return 404, "application/json", "{}", "page", None
        if path != base:
            return 200, "application/json", json.dumps({"prices": {"price": "3400", "currency_minor_unit": 2}}), "page", None
        page, per = int(q.get("page", 1)), int(q.get("per_page", 100))
        data = [woo_product(rid, i, host) for i in range((page - 1) * per, min(n, page * per))]
        return 200, "application/json", json.dumps(data), "page", None
    if a == "squarespace":
        if path == "/sitemap.xml":
            if n == 0:
                return 404, "text/html", "", "page", None
            return 200, "application/xml", sitemap(host, [f"/shop/p/p{i}" for i in range(n)] + ["/", "/about"]), "page", None
        if path == "/shop" and n:
            off = int(q.get("offset", 0))
            items = [sq_item(rid, i) for i in range(off, min(n, off + 20))]
            more = off + 20 < n
            return 200, "application/json", json.dumps({"items": items, "pagination": {"nextPage": more, "nextPageOffset": off + 20}}), "page", None
        return 404, "text/html", "", "page", None
    if a == "square_online":
        if path == "/":
            ids = f'"user":{{"id":"{rid}"}} ... "site_id":"{1000000 + rid}"' if n else "no store here"
            return 200, "text/html", f"<html><script>{ids}</script>" + "x" * 30000 + "</html>", "html", None
        return 404, "text/html", "", "page", None
    # generic
    if path == "/sitemap.xml":
        if n == 0 and p["nocat_mode"] == "404":
            return 404, "text/html", "", "page", None
        k = n if n else 12
        return 200, "application/xml", sitemap(host, [f"/products/coffee-{i}" for i in range(k)] + ["/", "/about"]), "page", None
    if path.startswith("/products/coffee-"):
        i = int(path.rsplit("-", 1)[1])
        if n == 0:
            return 200, "text/html", "<html>" + "x" * 80000 + "</html>", "html", None
        return 200, "text/html", jsonld_page(rid, i, host), "html", None
    return 404, "text/html", "", "page", None


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    headers = dict(scope["headers"])
    host = headers.get(b"host", b"").decode().split(":")[0]
    path = scope["path"]
    if path == "/__stats":
        status, ctype, body, kind, ra = 200, "application/json", json.dumps(counts), "robots", None
    else:
        q = {k: v[0] for k, v in parse_qs(scope["query_string"].decode()).items()}
        counts["req"] += 1
        status, ctype, body, kind, ra = route(host, path, q)
    await asyncio.sleep(latency(kind))
    hdrs = [(b"content-type", ctype.encode())]
    if ra is not None:
        hdrs.append((b"retry-after", str(ra).encode()))
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body.encode()})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False, backlog=2048)
