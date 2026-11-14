"""Find US roasters in open datasets, and probe each roaster's site for a usable catalog platform."""

from __future__ import annotations

import logging
import re
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from urllib.parse import urlparse

import httpx

from .http import USER_AGENT, Blocked, DeadlineReached, PoliteClient, RobotsUnavailable
from .text import scrub_surrogates

log = logging.getLogger(__name__)

OVERTURE = "https://overturemaps-us-west-2.s3.amazonaws.com/"
OVERPASS = "https://overpass-api.de/api/interpreter"
# Overpass answers a busy server with a 429 or 504 and asks clients to wait; each state is asked again after these
# pauses (seconds) before it counts as failed. One request at a time, as before.
OVERPASS_WAITS = (30.0, 90.0)
# An Overpass outage must not use up the discover job's 240 minutes (it runs before Overture): after this many
# states in a row go unanswered, or once the OSM pass has run this long, the states left are recorded as failed
# without asking. A whole pass normally takes minutes.
OVERPASS_DOWN_AFTER = 3
OSM_BUDGET_S = 60 * 60.0
# The Overture scan's DuckDB settings. Unset, DuckDB used every core it could see and peaked at 2.7 GiB of resident
# memory locally (four threads), in a job limited to 4Gi; two threads and a 1 GB buffer limit keep the scan far
# below that. The scan only filters and projects, so it needs no large buffers.
OVERTURE_DUCKDB: dict[str, str | int] = {"threads": 2, "memory_limit": "1GB"}
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
# aren't: they are recorded in the source list (`candidates`) for the owner to flag in the admin, never fetched here
# and never crawled unless flagged. Primary category or an alternate; open places only.
CANDIDATE_TAGS = ("coffee_shop", "cafe")

# One scan of the release for both lists: roasters (tagged coffee_roastery, or a coffee place with "roast" in the
# name, as before) and the café candidates (CANDIDATE_TAGS, open). From #130 (discover-cafes), recording only.
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
    SELECT website, name, city, state, confidence, cat = 'coffee_roastery' AS tagged,  -- primary: as before
           name ILIKE '%roast%' AS roast_in_name, is_roaster,
           CASE WHEN cat = 'coffee_shop' OR list_contains(alts, 'coffee_shop') THEN 'coffee_shop'
                WHEN cat = 'cafe' OR list_contains(alts, 'cafe') THEN 'cafe' END AS tag,
           operating_status IS NULL OR operating_status = 'open' AS open
    FROM r
    WHERE is_roaster OR tag IS NOT NULL"""


def _overture_urls(release: str) -> list[str]:
    xml = (
        urllib.request.urlopen(
            f"{OVERTURE}?list-type=2&prefix=release/{release}/theme=places/type=place/", timeout=30
        )
        .read()
        .decode()
    )
    return [OVERTURE + k for k in re.findall(r"<Key>([^<]+)</Key>", xml) if k.endswith(".parquet")]


def overture(release: str | None = None) -> list[dict]:
    """US places tagged coffee_roastery, or coffee places with "roast" in the name, one row per website."""
    return overture_places(release)[0]


def overture_places(release: str | None = None) -> tuple[list[dict], list[dict]]:
    """(roasters, places) from one read of the release (OVERTURE_SQL). roasters go to the registry, as before;
    places is the whole source list (`candidates`): every roaster and every open café or coffee shop, one row per
    storefront domain. Nothing is fetched from any of their sites."""
    import duckdb

    release = release or overture_latest_release()
    c = duckdb.connect(config=dict(OVERTURE_DUCKDB))
    c.sql("INSTALL httpfs; LOAD httpfs;")
    tags = "(" + ", ".join(f"'{t}'" for t in CANDIDATE_TAGS) + ")"
    rel = c.sql(
        OVERTURE_SQL.format(urls=_overture_urls(release), tags=tags, tag_list="[" + tags.strip("()") + "]")
    )
    return places_to_rows(rel.fetchall(), f"overture-{release}")


def places_to_rows(places: list[tuple], source: str) -> tuple[list[dict], list[dict]]:
    """OVERTURE_SQL's rows (website, name, city, state, confidence, tagged, roast_in_name, is_roaster, tag, open) as
    (roasters, places), each one row per normalized storefront domain (a chain is one row; `locations` counts its
    places). A place's `tags` are the tags its places carry (coffee_roastery as the primary category, as the
    registry's tagged_roastery; coffee_shop, cafe), sorted."""
    from .registry import is_storefront_host, normalize_domain

    def blank(d: str, name, city, state, website) -> dict:
        return {"roaster_id": d, "name": name, "city": city, "state": state, "locations": 0, "confidence": 0.0,
                "source": source, "website": website}  # fmt: skip

    def seen(row: dict, name, city, state, conf, website) -> None:
        row["locations"] += 1
        if (conf or 0) > row["confidence"]:
            row.update(name=name, city=city, state=state, confidence=round(conf or 0, 2), website=website)

    out: dict[str, dict] = {}
    listed: dict[str, dict] = {}
    for website, name, city, state, conf, tagged, roast, is_roaster, tag, is_open in places:
        d = normalize_domain(website or "")
        if not is_storefront_host(d) or not (is_roaster or (is_open and tag)):
            continue
        if is_roaster:
            row = out.setdefault(d, blank(d, name, city, state, website) | {"tagged_roastery": False,
                                                                           "roast_in_name": False})  # fmt: skip
            row["tagged_roastery"] |= bool(tagged)
            row["roast_in_name"] |= bool(roast)
            seen(row, name, city, state, conf, website)
        place = listed.setdefault(
            d, blank(d, name, city, state, website) | {"tags": [], "roast_in_name": False}
        )
        place["tags"] = sorted(
            set(place["tags"]) | ({"coffee_roastery"} if tagged else set()) | ({tag} if tag else set())
        )
        place["roast_in_name"] |= bool(roast)
        seen(place, name, city, state, conf, website)
    roasters = sorted(({k: v for k, v in r.items() if k != "website"} for r in out.values()),
                      key=lambda r: r["roaster_id"])  # fmt: skip
    return roasters, sorted(listed.values(), key=lambda r: r["roaster_id"])


# --- OpenStreetMap via Overpass (ODbL) ----------------------------------------------------------------


def _overpass(
    client: httpx.Client,
    st: str,
    sleep: Callable[[float], None] = time.sleep,
    stop: float | None = None,
) -> list[dict] | None:
    """One state's elements, or None if Overpass didn't answer it: an HTTP error or a busy server (429, 5xx) after
    OVERPASS_WAITS, or an answer whose `remark` reports a runtime error (a timed-out query returns 200 with the
    elements it found so far, which would be a silently short list). No wait runs past `stop` (time.monotonic())."""
    q = (
        f'[out:json][timeout:60];area["ISO3166-2"="US-{st}"]->.a;'
        '(nwr["craft"="coffee_roaster"](area.a);nwr["roastery"="yes"](area.a););out tags;'
    )
    for wait in (*OVERPASS_WAITS, None):
        try:
            r = client.post(OVERPASS, data={"data": q})
            r.raise_for_status()
            body = scrub_surrogates(r.json())
            remark = str(body.get("remark") or "")
            if "error" in remark.lower():
                raise ValueError(f"overpass remark: {remark[:200]}")
            return body.get("elements", [])
        except (httpx.HTTPError, ValueError) as e:
            # a transport error, a timed-out query or a busy server is worth asking again; another 4xx isn't
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            retryable = status is None or status == 429 or status >= 500
            out_of_time = wait is not None and stop is not None and time.monotonic() + wait >= stop
            if wait is None or not retryable or out_of_time:
                log.warning("overpass %s failed: %s", st, e)
                return None
            log.warning("overpass %s failed: %s; asking again in %.0fs", st, e, wait)
            sleep(wait)
    return None


def osm(
    states: list[str] | None = None, failed: list[str] | None = None, budget_s: float = OSM_BUDGET_S
) -> list[dict]:
    """craft=coffee_roaster / roastery=yes with a website, queried one state at a time. States Overpass didn't
    answer are skipped and, when `failed` is given, appended to it (the job records them). After
    OVERPASS_DOWN_AFTER unanswered states in a row, or `budget_s` seconds, the rest are failed without asking."""
    from .registry import is_storefront_host, normalize_domain

    out: dict[str, dict] = {}
    missed: list[str] = []
    in_a_row = 0
    stop = time.monotonic() + budget_s
    with httpx.Client(timeout=90, headers={"User-Agent": USER_AGENT}) as client:
        for st in states or US_STATES:
            if in_a_row >= OVERPASS_DOWN_AFTER or time.monotonic() >= stop:
                missed.append(st)  # Overpass is down, or the pass is out of time: not asked
                continue
            elements = _overpass(client, st, stop=stop)
            if elements is None:
                missed.append(st)
                in_a_row += 1
                continue
            in_a_row = 0
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
    if missed:
        log.warning("overpass: %d states not answered: %s", len(missed), ", ".join(missed))
    if failed is not None:
        failed.extend(missed)
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
    home = None
    no_robots = False
    for host in (domain, f"www.{domain}"):
        try:
            home = client.get(f"https://{host}/")
            break
        except Blocked:
            return {"status": "blocked", "final_host": host, "platform": "", "http_status": None}
        except RobotsUnavailable as e:
            if e.challenge:
                # an edge's challenge on robots.txt (a 202, or a 401/403 with a challenge header) answers every
                # request the same way: the site refuses us, as a robots block does. A probe status of its own that
                # counts as probed (`blocked`), so discovery doesn't ask again on every run (robots_unavailable does)
                return {"status": "blocked", "final_host": host, "platform": "", "http_status": e.status}
            if e.unreachable:  # the host itself is down, not just its robots.txt
                continue
            no_robots = True
        except httpx.HTTPError:
            continue
    if home is None:
        status = "robots_unavailable" if no_robots else "unreachable"
        return {"status": status, "final_host": "", "platform": "", "http_status": None}
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
        except RobotsUnavailable as e:  # the home page redirected to a host whose robots.txt didn't answer
            if e.challenge:  # refused, as above
                return {"status": "blocked", "final_host": host, "platform": "", "http_status": e.status}
            return {"status": "robots_unavailable", "final_host": host, "platform": "", "http_status": None}
        except (Blocked, httpx.HTTPError):
            data = None
        if isinstance(data, kind) and (kind is list or "products" in data):
            return {"status": "ok", "final_host": host, "platform": platform, "http_status": 200}
    html = home.text.lower()
    platform = next((p for p, pat in HTML_MARKERS if re.search(pat, html)), "other")
    return {"status": "ok", "final_host": host, "platform": platform, "http_status": 200}


# Probe rows are handed to `on_batch` (one lake file each) every PROBE_FLUSH_S seconds, or sooner at PROBE_BATCH rows:
# a full re-probe (about 3,200 roasters, under the job's 100-minute deadline) writes about 10 files, not one per 100
# rows (every crawl reads every probes file), and a killed run loses at most about 10 minutes of probing.
PROBE_BATCH = 1000
PROBE_FLUSH_S = 600.0


def probe(
    domains: list[str],
    workers: int = 16,
    run_date: str | None = None,
    deadline_s: float | None = None,
    on_batch: Callable[[list[dict]], object] | None = None,
    batch_size: int = PROBE_BATCH,
    flush_s: float = PROBE_FLUSH_S,
) -> list[dict]:
    """Probe roaster homepages; returns lake rows for the `probes` table (nothing is overwritten), one per roaster
    probed, in the order they finished.

    deadline_s: seconds from now after which no roaster is started and no request is sent; a roaster cut off part way
    gets no row (it was not probed, so the next run asks it again). on_batch: called with the rows finished so far
    every `flush_s` seconds or `batch_size` rows, and with the rest at the end, even when the run stops on an error
    (the original error is the one raised), so a caller that writes each batch keeps what was probed before a crash
    or a deadline."""
    run_date = run_date or date.today().isoformat()
    stop = None if deadline_s is None else time.monotonic() + deadline_s
    out: list[dict] = []
    pending: list[dict] = []

    def one(client: PoliteClient, domain: str) -> dict | None:
        if stop is not None and time.monotonic() >= stop:
            return None
        res = _safe_probe(client, domain)
        if res is None:
            return None
        now = datetime.now(UTC).replace(microsecond=0)
        return {"run_date": date.fromisoformat(run_date), "probed_at": now, "roaster_id": domain, **res}

    # Spaced like the catalog crawl (at least 1 s per host, or its robots.txt Crawl-delay), and robots.txt is read
    # and honoured first, as it is for every PoliteClient request.
    with (
        PoliteClient(min_interval=1.0, timeout=12, retries=2, deadline_s=deadline_s) as client,
        ThreadPoolExecutor(workers) as ex,
    ):
        futures = [ex.submit(one, client, d) for d in domains]
        flushed = time.monotonic()
        try:
            for f in as_completed(futures):
                row = f.result()
                if row is None:
                    continue
                out.append(row)
                pending.append(row)
                if on_batch is not None and (
                    len(pending) >= batch_size or time.monotonic() - flushed >= flush_s
                ):
                    batch, pending = pending, []
                    try:
                        on_batch(batch)
                    except BaseException:
                        pending = batch + pending  # not written: the flush below tries it again
                        raise
                    flushed = time.monotonic()
        except BaseException:
            for f in futures:
                f.cancel()  # start no more roasters
            if on_batch is not None and pending:
                try:
                    on_batch(pending)
                except Exception as e:  # noqa: BLE001 - the error that stopped the run is the one to raise
                    log.error("couldn't write %d probe rows after the run failed: %s", len(pending), e)
            raise
        if on_batch is not None and pending:
            on_batch(pending)
    return out


def _safe_probe(client: PoliteClient, domain: str) -> dict | None:
    """probe_one, or None when the run's deadline cut it off (no answer about the site, so no row)."""
    try:
        return probe_one(client, domain)
    except DeadlineReached:
        return None
    except Exception as e:  # noqa: BLE001 - one odd site must not abort the probe run
        log.warning("probe %s failed: %s", domain, e)
        return {"status": "unreachable", "final_host": "", "platform": "", "http_status": None}
