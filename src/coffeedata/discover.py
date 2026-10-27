"""Find US roasters in open datasets, and probe each roaster's site for a usable catalog platform."""

from __future__ import annotations

import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from urllib.parse import urlparse

import httpx

from .http import Blocked, PoliteClient

log = logging.getLogger(__name__)

OVERTURE = "https://overturemaps-us-west-2.s3.amazonaws.com/"
OVERPASS = "https://overpass-api.de/api/interpreter"
US_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "PR",
]


# --- Overture Maps Places (CDLA-Permissive-2.0) -------------------------------------------------------


def overture_latest_release() -> str:
    xml = urllib.request.urlopen(f"{OVERTURE}?list-type=2&prefix=release/&delimiter=/", timeout=30).read()
    return sorted(re.findall(r"<Prefix>release/([^<]+)/</Prefix>", xml.decode()))[-1]


def overture(release: str | None = None) -> list[dict]:
    """US places tagged coffee_roastery, or coffee places with "roast" in the name, one row per website."""
    import duckdb

    release = release or overture_latest_release()
    xml = (
        urllib.request.urlopen(
            f"{OVERTURE}?list-type=2&prefix=release/{release}/theme=places/type=place/", timeout=30
        )
        .read()
        .decode()
    )
    urls = [OVERTURE + k for k in re.findall(r"<Key>([^<]+)</Key>", xml) if k.endswith(".parquet")]
    c = duckdb.connect()
    c.sql("INSTALL httpfs; LOAD httpfs;")
    rel = c.sql(f"""
        WITH r AS (
          SELECT names.primary AS name, taxonomy.primary AS cat, addresses[1].locality AS city,
                 addresses[1].region AS state, confidence, websites[1] AS website
          FROM read_parquet({urls})
          WHERE ((bbox.xmin BETWEEN -125 AND -66 AND bbox.ymin BETWEEN 24 AND 50)
              OR (bbox.xmin BETWEEN -170 AND -129 AND bbox.ymin BETWEEN 51 AND 72)
              OR (bbox.xmin BETWEEN -161 AND -154 AND bbox.ymin BETWEEN 18 AND 23))
            AND addresses[1].country = 'US' AND websites IS NOT NULL
            AND (taxonomy.primary = 'coffee_roastery'
                 OR list_contains(taxonomy.alternates, 'coffee_roastery')
                 OR (names.primary ILIKE '%roast%'
                     AND (taxonomy.primary ILIKE '%coffee%' OR basic_category ILIKE '%coffee%'
                          OR names.primary ILIKE '%coffee%'))))
        SELECT website, name, city, state, confidence, cat = 'coffee_roastery' AS tagged,
               name ILIKE '%roast%' AS roast_in_name
        FROM r""")
    from .registry import is_storefront_host, normalize_domain

    out: dict[str, dict] = {}
    for website, name, city, state, conf, tagged, roast in rel.fetchall():
        d = normalize_domain(website or "")
        if not is_storefront_host(d):
            continue
        row = out.setdefault(
            d,
            {
                "roaster_id": d,
                "name": name,
                "city": city,
                "state": state,
                "locations": 0,
                "confidence": 0.0,
                "tagged_roastery": False,
                "roast_in_name": False,
                "source": f"overture-{release}",
            },
        )
        row["locations"] += 1
        if (conf or 0) > row["confidence"]:
            row.update(name=name, city=city, state=state, confidence=round(conf or 0, 2))
        row["tagged_roastery"] |= bool(tagged)
        row["roast_in_name"] |= bool(roast)
    return sorted(out.values(), key=lambda r: r["roaster_id"])


# --- OpenStreetMap via Overpass (ODbL) ----------------------------------------------------------------


def osm(states: list[str] | None = None) -> list[dict]:
    """craft=coffee_roaster / roastery=yes with a website, queried one state at a time."""
    from .registry import is_storefront_host, normalize_domain

    out: dict[str, dict] = {}
    with httpx.Client(timeout=90, headers={"User-Agent": "coffee-data/0.1"}) as client:
        for st in states or US_STATES:
            q = (
                f'[out:json][timeout:60];area["ISO3166-2"="US-{st}"]->.a;'
                '(nwr["craft"="coffee_roaster"](area.a);nwr["roastery"="yes"](area.a););out tags;'
            )
            try:
                r = client.post(OVERPASS, data={"data": q})
                r.raise_for_status()
                elements = r.json().get("elements", [])
            except (httpx.HTTPError, ValueError) as e:
                log.warning("overpass %s failed: %s", st, e)
                continue
            for el in elements:
                t = el.get("tags", {})
                d = normalize_domain(t.get("website") or t.get("contact:website") or "")
                if is_storefront_host(d):
                    out.setdefault(
                        d,
                        {
                            "roaster_id": d,
                            "name": t.get("name", ""),
                            "city": t.get("addr:city", ""),
                            "state": st,
                            "locations": 1,
                            "tagged_roastery": True,
                            "source": "osm",
                        },
                    )
    return sorted(out.values(), key=lambda r: r["roaster_id"])


# --- Platform probe ----------------------------------------------------------------------------------

HTML_MARKERS = [
    ("shopify?", r"cdn\.shopify\.com|shopify\.theme"),
    ("squarespace?", r"squarespace"),
    ("bigcommerce?", r"bigcommerce"),
    ("wix?", r"wix\.com|wixstatic"),
    ("woocommerce?", r"woocommerce"),
    ("square_online?", r"square\.site|editmysite"),
    ("webflow?", r"webflow"),
]


def probe_one(client: PoliteClient, domain: str) -> dict:
    """Which platform serves this roaster's catalog? status: ok | blocked | unreachable | http_<code>."""
    home = None
    for host in (domain, f"www.{domain}"):
        try:
            home = client.get(f"https://{host}/")
            break
        except Blocked:
            return {"status": "blocked", "final_host": host, "platform": "", "http_status": None}
        except httpx.HTTPError:
            continue
    if home is None:
        return {"status": "unreachable", "final_host": "", "platform": "", "http_status": None}
    if home.status_code != 200:
        return {
            "status": f"http_{home.status_code}",
            "final_host": urlparse(str(home.url)).netloc,
            "platform": "",
            "http_status": home.status_code,
        }
    host = urlparse(str(home.url)).netloc
    for platform, path, params, kind in [
        ("shopify", "/products.json", {"limit": 1}, dict),
        ("woocommerce", "/wp-json/wc/store/v1/products", {"per_page": 1}, list),
    ]:
        try:
            data = client.get_json(f"https://{host}{path}", **params)
        except (Blocked, httpx.HTTPError):
            data = None
        if isinstance(data, kind) and (kind is list or "products" in data):
            return {"status": "ok", "final_host": host, "platform": platform, "http_status": 200}
    html = home.text.lower()
    platform = next((p for p, pat in HTML_MARKERS if re.search(pat, html)), "other")
    return {"status": "ok", "final_host": host, "platform": platform, "http_status": 200}


def probe(domains: list[str], workers: int = 16, run_date: str | None = None) -> list[dict]:
    """Probe roaster homepages; returns lake rows for the `probes` table (nothing is overwritten)."""
    run_date = run_date or date.today().isoformat()
    with PoliteClient(min_interval=0.5, timeout=12, retries=2) as client, ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda d: _safe_probe(client, d), domains))
    now = datetime.now(UTC).replace(microsecond=0)
    return [
        {"run_date": date.fromisoformat(run_date), "probed_at": now, "roaster_id": d, **res}
        for d, res in zip(domains, results, strict=True)
    ]


def _safe_probe(client: PoliteClient, domain: str) -> dict:
    try:
        return probe_one(client, domain)
    except Exception as e:  # noqa: BLE001 - one odd site must not abort the probe run
        log.warning("probe %s failed: %s", domain, e)
        return {"status": "unreachable", "final_host": "", "platform": "", "http_status": None}
