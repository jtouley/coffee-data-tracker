"""Probe roaster websites: does it resolve, and which storefront platform serves its catalog?

usage: python probe.py in.csv out.csv   (in.csv needs a `domain` column; other columns pass through)
"""
import csv, json, re, sys, ssl, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

UA = "coffee-data/0.1 (+https://github.com/jtouley/coffee-data-tracker)"
CTX = ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")


def get(url, limit=400_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=12, context=CTX) as r:
            return r.status, r.geturl(), r.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:
        return 0, url, type(e).__name__


def norm_domain(d):
    d = d.strip().lower()
    if "://" in d:
        d = urlparse(d).netloc
    return d.split("/")[0].removeprefix("www.")


def probe(row):
    d = norm_domain(row["domain"])
    out = dict(row, domain=d, status="", final_host="", platform="", products="", note="")
    st, final, home = get(f"https://{d}/")
    if st == 0:
        st, final, home = get(f"https://www.{d}/")
    out["status"], out["final_host"] = st, urlparse(final).netloc
    if st == 0 or st >= 500:
        out["note"] = home[:60]
        return out
    host = out["final_host"] or d
    s, _, body = get(f"https://{host}/products.json?limit=1")
    if s == 200 and body.lstrip().startswith("{"):
        try:
            json.loads(body)["products"]; out["platform"] = "shopify"
            return out
        except Exception:
            pass
    s, _, body = get(f"https://{host}/wp-json/wc/store/v1/products?per_page=1")
    if s == 200 and body.lstrip().startswith("["):
        try:
            json.loads(body); out["platform"] = "woocommerce"
            return out
        except Exception:
            pass
    h = home.lower()
    for name, pat in [("shopify", r"cdn\.shopify\.com|shopify\.theme"), ("squarespace", r"squarespace"),
                      ("bigcommerce", r"bigcommerce"), ("wix", r"wix\.com|wixstatic"),
                      ("woocommerce", r"woocommerce"), ("square_online", r"square\.site|squareup|editmysite"),
                      ("webflow", r"webflow")]:
        if re.search(pat, h):
            out["platform"] = name + "?"  # detected from HTML; catalog endpoint not open
            break
    else:
        out["platform"] = "other" if st == 200 else f"http_{st}"
    return out


if __name__ == "__main__":
    rows = list(csv.DictReader(open(sys.argv[1])))
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(probe, rows))
    cols = list(res[0].keys())
    with open(sys.argv[2], "w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(res)
    from collections import Counter
    print(Counter(r["platform"] for r in res).most_common())
