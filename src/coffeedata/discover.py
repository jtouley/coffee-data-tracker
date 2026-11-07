"""Find US roasters in open datasets, and probe each roaster's site for a usable catalog platform."""

from __future__ import annotations

import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from urllib.parse import urlparse

import httpx

from .http import USER_AGENT, Blocked, PoliteClient, RobotsUnavailable
from .text import scrub_surrogates

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
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington dc": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}  # fmt: skip


def state_code(text: str) -> str:
    """A two-letter code from a code or full name ("OR", "Oregon", "D.C.", "Washington, DC"); "" if blank.
    Raises ValueError for anything else rather than guessing from the first two letters."""
    key = " ".join(text.replace(".", "").replace(",", " ").lower().split())
    if not key:
        return ""
    if key.upper() in US_STATES:
        return key.upper()
    if key in STATE_NAMES:
        return STATE_NAMES[key]
    raise ValueError(f"not a US state: {text!r}. Use the two-letter code, e.g. OR, DC or PR.")


# --- Overture Maps Places (CDLA-Permissive-2.0) -------------------------------------------------------


def overture_latest_release() -> str:
    xml = urllib.request.urlopen(f"{OVERTURE}?list-type=2&prefix=release/&delimiter=/", timeout=30).read()
    return sorted(re.findall(r"<Prefix>release/([^<]+)/</Prefix>", xml.decode()))[-1]


# Places tagged as cafés rather than roasteries. Some are real roasters (Rival Bros is tagged only coffee_shop), most
# aren't: they're recorded as screening candidates (screen.py), never straight into the registry, so the weekly crawl
# sees one only after its screen admits it. Primary category or an alternate; open places only.
CANDIDATE_TAGS = ("coffee_shop", "cafe")


def _overture_urls(release: str) -> list[str]:
    xml = (
        urllib.request.urlopen(
            f"{OVERTURE}?list-type=2&prefix=release/{release}/theme=places/type=place/", timeout=30
        )
        .read()
        .decode()
    )
    return [OVERTURE + k for k in re.findall(r"<Key>([^<]+)</Key>", xml) if k.endswith(".parquet")]


# One scan of the release for both lists: roasters (tagged coffee_roastery, or a coffee place with "roast" in the
# name, as before) and screening candidates (CANDIDATE_TAGS, open, and not a roaster by that rule).
OVERTURE_SQL = """
    WITH r AS (
      SELECT names.primary AS name, taxonomy.primary AS cat, coalesce(taxonomy.alternates, []) AS alts,
             addresses[1].locality AS city, addresses[1].region AS state, confidence, websites[1] AS website,
             operating_status,
             (taxonomy.primary = 'coffee_roastery'
              OR list_contains(taxonomy.alternates, 'coffee_roastery')
              OR (names.primary ILIKE '%roast%'
                  AND (taxonomy.primary ILIKE '%coffee%' OR basic_category ILIKE '%coffee%'
                       OR names.primary ILIKE '%coffee%'))) AS is_roaster
      FROM read_parquet({urls}, hive_partitioning = false)
      WHERE ((bbox.xmin BETWEEN -125 AND -66 AND bbox.ymin BETWEEN 24 AND 50)
          OR (bbox.xmin BETWEEN -170 AND -129 AND bbox.ymin BETWEEN 51 AND 72)
          OR (bbox.xmin BETWEEN -161 AND -154 AND bbox.ymin BETWEEN 18 AND 23))
        AND addresses[1].country = 'US' AND websites IS NOT NULL
        AND (taxonomy.primary IN {tags} OR list_has_any(taxonomy.alternates, {tag_list})
             OR taxonomy.primary = 'coffee_roastery' OR list_contains(taxonomy.alternates, 'coffee_roastery')
             OR names.primary ILIKE '%roast%'))
    SELECT website, name, city, state, confidence, cat = 'coffee_roastery' AS tagged,
           name ILIKE '%roast%' AS roast_in_name, is_roaster,
           CASE WHEN cat = 'coffee_shop' OR list_contains(alts, 'coffee_shop') THEN 'coffee_shop'
                WHEN cat = 'cafe' OR list_contains(alts, 'cafe') THEN 'cafe' END AS tag,
           operating_status IS NULL OR operating_status = 'open' AS open
    FROM r
    WHERE is_roaster OR tag IS NOT NULL"""


def overture(release: str | None = None) -> list[dict]:
    """US places tagged coffee_roastery, or coffee places with "roast" in the name, one row per website."""
    return overture_places(release)[0]


def overture_places(release: str | None = None) -> tuple[list[dict], list[dict]]:
    """(roasters, candidates) from one read of the release: see OVERTURE_SQL. Both are one row per website domain
    that passes is_storefront_host, so a chain is one row (its `locations` counts the places). A domain that is a
    roaster by the rule is never also a candidate."""
    import duckdb

    release = release or overture_latest_release()
    c = duckdb.connect()
    c.sql("INSTALL httpfs; LOAD httpfs;")
    tags = "(" + ", ".join(f"'{t}'" for t in CANDIDATE_TAGS) + ")"
    rel = c.sql(
        OVERTURE_SQL.format(urls=_overture_urls(release), tags=tags, tag_list="[" + tags.strip("()") + "]")
    )
    return places_to_rows(rel.fetchall(), f"overture-{release}")


def places_to_rows(places: list[tuple], source: str) -> tuple[list[dict], list[dict]]:
    """OVERTURE_SQL's rows (website, name, city, state, confidence, tagged, roast_in_name, is_roaster, tag, open) as
    (roasters, candidates), one row per normalized storefront domain."""
    from .registry import is_storefront_host, normalize_domain

    out: dict[str, dict] = {}
    cand: dict[str, dict] = {}
    for website, name, city, state, conf, tagged, roast, is_roaster, tag, is_open in places:
        d = normalize_domain(website or "")
        if not is_storefront_host(d):
            continue
        if is_roaster:
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
                    "source": source,
                },
            )
            row["tagged_roastery"] |= bool(tagged)
            row["roast_in_name"] |= bool(roast)
        elif is_open and tag:
            row = cand.setdefault(
                d,
                {
                    "roaster_id": d,
                    "name": name,
                    "city": city,
                    "state": state,
                    "locations": 0,
                    "confidence": 0.0,
                    "tag": tag,
                    "source": source,
                },
            )
            if tag == "coffee_shop":  # a domain with any coffee_shop place is in that stratum
                row["tag"] = tag
        else:
            continue
        row["locations"] += 1
        if (conf or 0) > row["confidence"]:
            row.update(name=name, city=city, state=state, confidence=round(conf or 0, 2))
    roasters = sorted(out.values(), key=lambda r: r["roaster_id"])
    candidates = sorted((r for d, r in cand.items() if d not in out), key=lambda r: r["roaster_id"])
    return roasters, candidates


# --- OpenStreetMap via Overpass (ODbL) ----------------------------------------------------------------


def osm(states: list[str] | None = None) -> list[dict]:
    """craft=coffee_roaster / roastery=yes with a website, queried one state at a time."""
    from .registry import is_storefront_host, normalize_domain

    out: dict[str, dict] = {}
    with httpx.Client(timeout=90, headers={"User-Agent": USER_AGENT}) as client:
        for st in states or US_STATES:
            q = (
                f'[out:json][timeout:60];area["ISO3166-2"="US-{st}"]->.a;'
                '(nwr["craft"="coffee_roaster"](area.a);nwr["roastery"="yes"](area.a););out tags;'
            )
            try:
                r = client.post(OVERPASS, data={"data": q})
                r.raise_for_status()
                elements = scrub_surrogates(r.json()).get("elements", [])
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
    """Which platform serves this roaster's catalog? status: ok | blocked (robots.txt disallows us, or answers with
    an edge's challenge) | unreachable | http_<code> |
    robots_unavailable (robots.txt didn't answer: no answer about the site, so the next probe run asks again)."""
    return probe_home(client, domain)[0]


def probe_home(client: PoliteClient, domain: str) -> tuple[dict, str]:
    """probe_one's answer, and the home page's text when the probe is ok ("" otherwise): the screen (screen.py)
    reads it for roastery wording without asking for the page again."""
    res, home = _probe(client, domain)
    return res, home.text if home is not None and res["status"] == "ok" else ""


def _probe(client: PoliteClient, domain: str) -> tuple[dict, httpx.Response | None]:
    home = None
    no_robots = False
    answered: httpx.HTTPStatusError | None = None  # a 429 or 5xx the client's retries didn't get past
    for host in (domain, f"www.{domain}"):
        try:
            home = client.get(f"https://{host}/")
            break
        except Blocked:
            return {"status": "blocked", "final_host": host, "platform": "", "http_status": None}, home
        except RobotsUnavailable as e:
            if e.challenge:
                # an edge's challenge on robots.txt (a 202, or a 401/403 with a challenge header) answers every
                # request the same way: the site refuses us, as a robots block does. A probe status of its own that
                # counts as probed (`blocked`), so discovery doesn't ask again on every run (robots_unavailable does)
                return {
                    "status": "blocked",
                    "final_host": host,
                    "platform": "",
                    "http_status": e.status,
                }, home
            if e.unreachable:  # the host itself is down, not just its robots.txt
                continue
            no_robots = True
        except httpx.HTTPStatusError as e:
            answered = e
        except httpx.HTTPError:
            continue
    if home is None and answered is not None:
        # The site answered, with a pause or a server error: http_<code>, not unreachable, so whoever reads the
        # probe (the screen's re-screen window) can tell a busy site from a dead one.
        code = answered.response.status_code
        return {"status": f"http_{code}", "final_host": answered.request.url.host, "platform": "",
                "http_status": code}, home  # fmt: skip
    if home is None:
        status = "robots_unavailable" if no_robots else "unreachable"
        return {"status": status, "final_host": "", "platform": "", "http_status": None}, home
    if home.status_code != 200:
        return {
            "status": f"http_{home.status_code}",
            "final_host": urlparse(str(home.url)).netloc,
            "platform": "",
            "http_status": home.status_code,
        }, home
    host = urlparse(str(home.url)).netloc
    for platform, path, params, kind in [
        ("shopify", "/products.json", {"limit": 1}, dict),
        ("woocommerce", "/wp-json/wc/store/v1/products", {"per_page": 1}, list),
    ]:
        try:
            data = client.get_json(f"https://{host}{path}", **params)
        except RobotsUnavailable as e:  # the home page redirected to a host whose robots.txt didn't answer
            if e.challenge:  # refused, as above
                return {
                    "status": "blocked",
                    "final_host": host,
                    "platform": "",
                    "http_status": e.status,
                }, home
            return {
                "status": "robots_unavailable",
                "final_host": host,
                "platform": "",
                "http_status": None,
            }, home
        except (Blocked, httpx.HTTPError):
            data = None
        if isinstance(data, kind) and (kind is list or "products" in data):
            return {"status": "ok", "final_host": host, "platform": platform, "http_status": 200}, home
    html = home.text.lower()
    platform = next((p for p, pat in HTML_MARKERS if re.search(pat, html)), "other")
    return {"status": "ok", "final_host": host, "platform": platform, "http_status": 200}, home


def probe(domains: list[str], workers: int = 16, run_date: str | None = None) -> list[dict]:
    """Probe roaster homepages; returns lake rows for the `probes` table (nothing is overwritten)."""
    run_date = run_date or date.today().isoformat()
    # Spaced like the catalog crawl (at least 1 s per host, or its robots.txt Crawl-delay), and robots.txt is read
    # and honoured first, as it is for every PoliteClient request.
    with PoliteClient(min_interval=1.0, timeout=12, retries=2) as client, ThreadPoolExecutor(workers) as ex:
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
