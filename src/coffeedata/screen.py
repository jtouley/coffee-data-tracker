"""The one-time screen that admits a discovery candidate to the registry.

Overture tags most real roasters `coffee_roastery`, but some only `coffee_shop` or `cafe` (Rival Bros), alongside
tens of thousands of cafés that roast nothing. Discovery records those places as candidates (the `candidates`
table), never as registry roasters, so the weekly crawl doesn't see them. The screen visits each candidate once,
politely, through the probe and the catalog adapters, and admits it only when it looks like an actual roaster that
sells its own coffee online:

1. the probe (discover.probe_home): robots.txt, the home page, the platform;
2. the public product list through the platform's adapter, read up to PAGE_BUDGET pages (a partial read counts);
3. the rules (classify.is_coffee, no verdicts) count the coffees, and cheap signals say whether the store roasts
   them or resells another roaster's: roastery wording on the home page or in the coffees' descriptions, the
   coffees' vendor (Shopify) naming the store's own brand or another roaster we know, and "we proudly serve X"
   wording.

RULES holds the candidate decision rules; RULE is the one in force. Each screen is recorded in `screens` with every
signal, the rule and its reason, so it can be audited and rescored offline under another rule
(eval/screen/README.md). Admitted candidates join the registry as discovered roasters do (a `registry` row, plus the
screen's probe as a `probes` row), so the next crawl picks them up. Cafés and shops that resell other roasters' beans
are deliberately out of scope: when the signals can't tell, the screen rejects.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from . import classify, discover, lineage
from .adapters import fetch_catalog
from .http import DeadlineReached, FetchError, PartialCatalog, PoliteClient
from .lake import Lake, new_batch_id, retry_transient
from .models import CatalogItem, Roaster
from .origins import COUNTRIES
from .text import html_to_text

log = logging.getLogger(__name__)

# At least this many products the rules call coffee. Provisional until chosen on the validation set
# (eval/screen/README.md); the investigation's rule was "at least one".
MIN_COFFEES = 1
# A rejection is an answer about the site, asked again only after RESCREEN_DAYS: never-screened candidates go first,
# so a re-screen only spends a run's spare capacity. 180 days because a café that starts roasting and selling bags
# online is a rare, slow change (a new roastery and a new store), and a monthly run screens a few thousand of the
# ~43,600 candidates, so the first pass alone takes most of a year; a shorter window would only re-ask the same
# cafés sooner. A failure that may pass (RETRY_STATUSES) is asked again after RETRY_DAYS, as the probe re-probes
# stale roasters.
RESCREEN_DAYS = 180
RETRY_DAYS = 28
# Catalog pages one screen may request (robots.txt not counted), and the most time one screen may take: a Shopify
# store's first 250 products, a WooCommerce store's first 100, a Squarespace sitemap and shop collections, or a
# generic store's sitemaps and its first product pages (bean-looking first). A partial read is enough for a count.
PAGE_BUDGET = 8
SCREEN_TIMEOUT_S = 120.0
HOME_TEXT_CHARS = 50_000  # of the home page's text, searched for roastery wording
# The monthly discover job's share of its 240-minute limit (src/terraform/ingestion.tf): Overture and OSM discovery
# take well under an hour, and publishing after each chunk keeps what was screened if the job is stopped.
DEADLINE_MIN = 140.0
MAX_PER_RUN = 6000
CHUNK = 200
WORKERS = 16

# Failures that may pass soon (a server error, robots.txt not answering, a catalog read that failed): screened again
# after RETRY_DAYS. Every other screen, an unreachable site included (most are dead domains), is an answer about the
# site, screened again after RESCREEN_DAYS. A screen cut off by the run's deadline is no screen at all.
RETRY_STATUSES = ("robots_unavailable", "error")
RETRY_REASONS = ("error", "catalog_error")

_GENERIC = {
    "coffee", "coffees", "roast", "roasters", "roaster", "roasting", "roastery", "roasted", "roastworks", "co", "company",
    "cafe", "caffe", "the", "and", "house", "shop", "llc", "inc", "bar", "espresso", "tea", "teas", "kitchen",
    "bakery", "of", "a", "beans", "bean", "brew", "brewing", "café", "lab", "works", "trading", "supply",
}  # fmt: skip

# Wording that the store roasts its own coffee.
ROAST_WORDS = re.compile(
    r"\b(we roast|we(?:'re| are) (?:a |an )?(?:small[- ]batch |independent |specialty |local |family[- ]owned )*"
    r"(?:coffee )?roast(?:er|ery)|roasted (?:in[- ]house|on[- ]site|by us|here|right here|in our)|"
    r"(?:our|own) roast(?:ery|ing (?:facility|plant|room))|roast(?:ed|ing)? (?:all )?(?:of )?our own|"
    r"in[- ]house roast(?:ing|ed|ery)?|roast(?:ed|ing) (?:it )?ourselves|we source and roast|sourced and roasted by us|"
    r"micro[- ]?roast(?:er|ery)|small[- ]batch roast(?:er|ery|ed|ing))\b",
    re.I,
)
# Wording that a café serves, or a shop sells, another roaster's coffee.
SERVES_WORDS = re.compile(
    r"\b(proudly (?:serv(?:e|es|ing)|brew(?:s|ing)?|pour(?:s|ing)?)|we serve [A-Z]|featuring (?:coffee|beans) from|"
    r"(?:our|local) (?:coffee )?roasting partners?|partner roaster|coffee (?:is )?(?:roasted|supplied) by|"
    r"roasted by (?:our friends|the (?:folks|team|good people))|we carry (?:beans|coffee) from)\b",
    re.I,
)


def brand_key(name: str) -> str:
    """A name's distinctive words, lower case, accents and punctuation dropped: "Rival Bros Coffee" -> "rival bros"."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("'", "")
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _GENERIC]
    return " ".join(words)


def _squash(s: str) -> str:
    return s.replace(" ", "")


def domain_stem(domain: str) -> str:
    """The domain's name part: "rivalbros.com" -> "rivalbros", "shop.example.co.uk" -> "example"."""
    parts = domain.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net"):
        parts = parts[:-1]
    return parts[-2] if len(parts) >= 2 else parts[0]


def is_own(vendor: str, name: str, domain: str) -> bool:
    """A vendor that names the store itself: its brand key, or its domain's name."""
    v = _squash(brand_key(vendor))
    if len(v) < 3:
        return False
    own = {_squash(brand_key(name)), domain_stem(domain)}
    return any(o and (v == o or (len(v) >= 4 and v in o) or (len(o) >= 4 and o in v)) for o in own)


# Words that name a coffee rather than a business, so a known roaster's name made only of them ("Dark Roast", "Single
# Origin", "Honduras Coffee") is never matched in another store's titles.
_COFFEE_VOCAB = {
    "light", "medium", "dark", "french", "italian", "vienna", "city", "full", "blend", "blends", "single", "origin",
    "decaf", "organic", "fair", "trade", "local", "house", "morning", "breakfast", "signature", "classic", "cold",
    "whole", "ground", "fresh", "craft", "specialty", "small", "batch", "premium", "gourmet", "estate", "reserve",
}  # fmt: skip


class KnownRoasters:
    """Brand keys of the roasters we already know (the registry), to tell a vendor or title naming another roaster. A
    vendor must name one whole. In a title only a key of two or more words counts, and not one made only of coffee
    words or origins: single words ("Autumn", "Local", "Origin") are blend names as often as roasters."""

    def __init__(self, names: Iterable[str]):
        vocab = _COFFEE_VOCAB | {w for c in COUNTRIES for w in brand_key(c).split()}
        self.keys = {k for k in (brand_key(n) for n in names) if len(_squash(k)) >= 3}
        title_keys = sorted(
            (k for k in self.keys if len(k.split()) >= 2 and not set(k.split()) <= vocab),
            key=len,
            reverse=True,
        )
        self._title = (
            re.compile(r"\b(" + "|".join(re.escape(k) for k in title_keys) + r")\b") if title_keys else None
        )

    def vendor(self, vendor: str, own: str) -> bool:
        k = brand_key(vendor)
        return bool(k) and k != own and k in self.keys

    def in_title(self, title: str, own: str) -> str | None:
        if self._title is None:
            return None
        for m in self._title.finditer(brand_key(title)):
            if m.group(1) != own and m.group(1) not in own:
                return m.group(1)
        return None


# A coffee named as one in its title, type or tags. On a café's or gift shop's store the rules, tuned on roasters'
# catalogs, also call jams, dessert bars and gift boxes coffee ("Holiday Hits", "Sugar Plum Jam": train set); a
# roaster's coffees nearly always say coffee, espresso, roast, blend, decaf or beans, or name an origin.
_NAMED = re.compile(
    r"\b(coffees?|espresso|roast(?:ed)?|blend|decaf|beans?|whole[- ]bean|single[- ]origin|"
    + "|".join(re.escape(c) for c in COUNTRIES)
    + r")\b",
    re.I,
)


def named_coffee(item: CatalogItem) -> bool:
    return bool(_NAMED.search(" | ".join([item.title, item.product_type, *item.tags])))


@dataclass
class Signals:
    products: int
    coffees: int  # products the rules (classify.is_coffee) call coffee
    named: int  # of those, the ones named as coffee (named_coffee)
    own_vendor: int  # named coffees whose vendor names the store
    other_roaster: int  # named coffees whose vendor or title names another known roaster
    roast_phrase: str  # the first roastery wording found ("" if none)
    serves_phrase: str  # the first "proudly serving X" wording found


def signals(
    items: list[CatalogItem], home_text: str, name: str, domain: str, known: KnownRoasters
) -> Signals:
    coffees = [i for i in items if classify.is_coffee(i)]
    named = [i for i in coffees if named_coffee(i)]
    own = brand_key(name)
    own_vendor = sum(is_own(i.vendor, name, domain) for i in named)
    other = sum(
        (not is_own(i.vendor, name, domain) and known.vendor(i.vendor, own))
        or bool(known.in_title(i.title, own))
        for i in named
    )
    text = " ".join([home_text, *(i.body_text[:1500] for i in named[:20])])
    roast = ROAST_WORDS.search(text)
    serves = SERVES_WORDS.search(text)
    return Signals(
        products=len(items),
        coffees=len(coffees),
        named=len(named),
        own_vendor=own_vendor,
        other_roaster=other,
        roast_phrase=roast.group(0)[:80] if roast else "",
        serves_phrase=serves.group(0)[:80] if serves else "",
    )


# --- decision rules ------------------------------------------------------------------------------------------------
# Each takes the signals and the minimum coffee count and returns (admitted, reason). The reason names the first test
# that failed, or the evidence that admitted. Every rule rejects a store with too few coffees.


def _too_few(s: Signals, n: int, count: int) -> tuple[bool, str] | None:
    if s.products == 0:
        return False, "no_catalog"
    if count < n:
        return False, f"no_coffee: {s.coffees} coffees ({s.named} named) of {s.products} products"
    return None


def _reseller(s: Signals) -> str | None:
    """Half its coffees name another roaster, or it says it serves another's coffee and never that it roasts."""
    if 2 * s.other_roaster >= s.named > 0:
        return f"reseller: {s.other_roaster} of {s.named} coffees name another roaster"
    if s.serves_phrase and not s.roast_phrase:
        return f"reseller: says {s.serves_phrase!r}"
    return None


def rule_coffee(s: Signals, n: int) -> tuple[bool, str]:
    """The investigation's rule: any store the rules find coffee in."""
    return _too_few(s, n, s.coffees) or (True, f"coffee: {s.coffees} coffees")


def rule_not_reseller(s: Signals, n: int) -> tuple[bool, str]:
    """Named coffee, and no sign it resells another roaster's."""
    if r := _too_few(s, n, s.named):
        return r
    if why := _reseller(s):
        return False, why
    return True, f"not_reseller: {s.named} coffees"


def rule_own_brand(s: Signals, n: int) -> tuple[bool, str]:
    """Named coffee, no sign of reselling, and evidence the coffee is the store's own: roastery wording, or at least
    half its coffees under its own name as vendor."""
    if r := _too_few(s, n, s.named):
        return r
    if why := _reseller(s):
        return False, why
    if s.roast_phrase:
        return True, f"roaster: {s.named} coffees; says {s.roast_phrase!r}"
    if 2 * s.own_vendor >= s.named:
        return True, f"own_brand: {s.own_vendor} of {s.named} coffees under its own name"
    return False, "not_roaster: no roastery wording and no own-brand coffees"


def rule_roast_words(s: Signals, n: int) -> tuple[bool, str]:
    """Named coffee, no sign of reselling, and roastery wording: the strictest."""
    if r := _too_few(s, n, s.named):
        return r
    if why := _reseller(s):
        return False, why
    if s.roast_phrase:
        return True, f"roaster: {s.named} coffees; says {s.roast_phrase!r}"
    return False, "not_roaster: no roastery wording"


RULES: dict[str, Callable[[Signals, int], tuple[bool, str]]] = {
    "coffee": rule_coffee,
    "not_reseller": rule_not_reseller,
    "own_brand": rule_own_brand,
    "roast_words": rule_roast_words,
}
# Provisional until chosen on the validation set (eval/screen/README.md). When unsure, reject: own_brand admits only
# with evidence the coffee is the store's own.
RULE = "own_brand"


# --- one screen ----------------------------------------------------------------------------------------------------


class BudgetSpent(DeadlineReached):
    """One screen's page budget or time limit is spent: what it read is its answer (adapters return it as partial)."""


class ScreenClient(PoliteClient):
    """A PoliteClient whose catalog requests, in one thread, stop after a page budget or a time limit (DeadlineReached,
    which every adapter turns into a partial catalog of what it read). robots.txt is never counted or stopped, so a
    host's rules are always known before anything is asked of it."""

    def _send_retrying(self, url: str, params: dict | None = None, page: bool = True) -> httpx.Response:
        b = getattr(self._local, "budget", None)
        if page and b is not None:
            if b["left"] <= 0 or time.monotonic() >= b["until"]:
                raise BudgetSpent(f"the screen's budget is spent before {url}")
            b["left"] -= 1
        return super()._send_retrying(url, params, page)

    def budget(self, pages: int, seconds: float) -> _Budget:
        return _Budget(self._local, pages, seconds)


class _Budget:
    def __init__(self, local: threading.local, pages: int, seconds: float):
        self.local, self.pages, self.seconds = local, pages, seconds

    def __enter__(self) -> None:
        self.local.budget = {"left": self.pages, "until": time.monotonic() + self.seconds}

    def __exit__(self, *exc: object) -> None:
        self.local.budget = None


def screen_one(
    client: ScreenClient,
    domain: str,
    name: str,
    known: KnownRoasters,
    rule: str = RULE,
    min_coffees: int = MIN_COFFEES,
    keep: dict | None = None,
) -> dict:
    """One candidate's screen: the probe, a budgeted read of its product list, the signals and the rule's answer. No
    exception escapes: one odd site is that site's `error`, never the run's. keep: given a dict, the products read
    and the home page's text are put in it, so an evaluation can rescore the same evidence under another rule
    (decide)."""
    start = time.monotonic()
    out: dict = {"products": 0, "coffees": 0, "named": 0, "own_vendor": 0, "other_roaster": 0, "partial": False,
                 "roast_phrase": "", "serves_phrase": "", "rule": rule, "min_coffees": min_coffees}  # fmt: skip
    with client.track() as stats:
        try:
            with client.budget(PAGE_BUDGET + 4, SCREEN_TIMEOUT_S):  # the probe's own requests: at most 4
                probe, home = discover.probe_home(client, domain)
            out |= probe
            if probe["status"] != "ok":
                out |= {"admitted": False, "reason": probe["status"]}
            else:
                roaster = Roaster(
                    roaster_id=domain,
                    name=name or domain,
                    host=probe["final_host"],
                    platform=probe["platform"],
                )
                with client.budget(PAGE_BUDGET, SCREEN_TIMEOUT_S):
                    try:
                        items = fetch_catalog(client, roaster)
                    except PartialCatalog as e:
                        if _run_deadline(e):
                            raise DeadlineReached(str(e)) from e
                        items, out["partial"] = list(e.items), True
                text = html_to_text(home)[:HOME_TEXT_CHARS]
                if keep is not None:
                    keep.update(items=items, home_text=text)
                out |= decide(items, text, name, domain, known, rule, min_coffees)
        except DeadlineReached as e:
            # the run's deadline, not this screen's budget: no answer, screened again next run
            if _run_deadline(e):
                out |= {"status": "deadline", "admitted": False, "reason": f"deadline: {e}"[:300]}
            else:  # its budget went on the probe or a sitemap before any product: an answer, as no_catalog is
                out |= {"status": out.get("status") or "budget_spent", "admitted": False,
                        "reason": f"budget_spent: {e}"[:300]}  # fmt: skip
        except FetchError as e:
            out |= {
                "status": out.get("status") or "error",
                "admitted": False,
                "reason": f"catalog_error: {e}"[:300],
            }
        except Exception as e:  # noqa: BLE001 - one odd site must not abort the screen run
            log.warning("screen %s failed: %s", domain, e)
            out |= {"status": out.get("status") or "error", "admitted": False,
                    "reason": f"error: {type(e).__name__}: {e}"[:300]}  # fmt: skip
    out.setdefault("final_host", "")
    out.setdefault("platform", "")
    out.setdefault("http_status", None)
    out["requests"] = stats.requests
    out["duration_s"] = round(time.monotonic() - start, 3)
    return out


def decide(
    items: list[CatalogItem],
    home_text: str,
    name: str,
    domain: str,
    known: KnownRoasters,
    rule: str = RULE,
    min_coffees: int = MIN_COFFEES,
) -> dict:
    """The signals of a store's products and home page text, and the rule's answer (admitted, reason)."""
    s = signals(items, home_text, name, domain, known)
    admitted, reason = RULES[rule](s, min_coffees)
    return {k: getattr(s, k) for k in Signals.__dataclass_fields__} | {"admitted": admitted, "reason": reason}


def _run_deadline(e: BaseException) -> bool:
    """A DeadlineReached (or the PartialCatalog an adapter made of it) from the run's deadline, not BudgetSpent."""
    from .http import causes

    hits = [c for c in causes(e) if isinstance(c, DeadlineReached)]
    return bool(hits) and not any(isinstance(c, BudgetSpent) for c in hits)


# --- which candidates, and the run ---------------------------------------------------------------------------------

CANDIDATES_SQL = """
select roaster_id, arg_max(name, (added_at, name)) as name, arg_max(source, (added_at, source)) as source,
       arg_max(tag, (added_at, tag)) as tag, arg_max(city, (added_at, city)) as city,
       arg_max(state, (added_at, state)) as state, max(locations) as locations, max(confidence) as confidence
from {candidates} where roaster_id is not null group by roaster_id
"""
LAST_SCREEN_SQL = """
select roaster_id, screened_at, admitted, status, reason from {screens}
where roaster_id is not null and status <> 'deadline'
qualify row_number() over (partition by roaster_id order by screened_at desc, batch_id desc) = 1
"""


def order_key(domain: str) -> str:
    """A stable order over the pool: each run continues where the last stopped, and a run that is cut short leaves
    the same candidates for the next."""
    return hashlib.sha256(f"coffeedata-screen:{domain}".encode()).hexdigest()


def due(last: dict | None, run_date: date) -> bool:
    """Whether a candidate's last screen (None: never screened) is due again on run_date."""
    if last is None:
        return True
    if last["admitted"]:
        return False  # admitted ones are registry roasters from then on (select drops them)
    age = run_date - last["screened_at"].date()
    status, reason = str(last["status"]), str(last["reason"])
    answered = not (
        status in RETRY_STATUSES or status.startswith("http_5") or reason.startswith(RETRY_REASONS)
    )
    return age >= timedelta(days=RESCREEN_DAYS if answered else RETRY_DAYS)


def select(
    lake: Lake, run_date: str, known: set[str], cap: int | None = MAX_PER_RUN, only: str | None = None
) -> list[dict]:
    """The candidates due on run_date, not already roasters (known: every registry roaster, seed and lake), never
    screened first in order_key's order, then the re-screens, oldest first; at most cap. only: that one candidate,
    due or not (not one that is already a roaster)."""
    from .registry import not_a_roaster_site

    con = lake.connect()
    cands = retry_transient(
        lambda: con.execute(CANDIDATES_SQL.format(candidates=lake.scan("candidates"))).fetchall()
    )
    cols = ("roaster_id", "screened_at", "admitted", "status", "reason")
    last = {
        r[0]: dict(zip(cols, r, strict=True))
        for r in retry_transient(
            lambda: con.execute(LAST_SCREEN_SQL.format(screens=lake.scan("screens"))).fetchall()
        )
    }
    rd = date.fromisoformat(run_date)
    keys = ("roaster_id", "name", "source", "tag", "city", "state", "locations", "confidence")
    todo = [
        dict(zip(keys, c, strict=True))
        for c in cands
        if c[0] not in known
        and not not_a_roaster_site(c[0])
        and (c[0] == only if only else due(last.get(c[0]), rd))
    ]
    todo.sort(
        key=lambda c: (
            c["roaster_id"] in last,
            last[c["roaster_id"]]["screened_at"] if c["roaster_id"] in last else 0,
            order_key(c["roaster_id"]),
        )
    )
    return todo[:cap] if cap is not None else todo


def run(
    lake: Lake,
    todo: list[dict],
    known_names: Iterable[str],
    run_date: str,
    workers: int = WORKERS,
    deadline_min: float = DEADLINE_MIN,
    rule: str = RULE,
    min_coffees: int = MIN_COFFEES,
) -> dict:
    """Screen `todo` (select's rows), CHUNK at a time: each chunk's screens, and its admissions (registry and
    probes rows), are written and published together, so a stopped job keeps what it screened. No candidate is
    started after deadline_min; one still running then stops at its next request."""
    known = KnownRoasters(known_names)
    until = time.monotonic() + 60 * deadline_min
    version = lineage.code_version()
    totals = {"screened": 0, "admitted": 0, "skipped_deadline": 0}
    # spaced as the probe and the crawl are: at least 1 s per host, or its robots.txt Crawl-delay
    client = ScreenClient(min_interval=1.0, timeout=12, retries=2, deadline_s=60 * deadline_min)
    with client:
        for i in range(0, len(todo), CHUNK):
            chunk = todo[i : i + CHUNK]
            if time.monotonic() >= until:
                totals["skipped_deadline"] += len(todo) - i
                break
            with ThreadPoolExecutor(workers) as ex:
                results = list(
                    ex.map(
                        lambda c: (
                            None
                            if time.monotonic() >= until
                            else screen_one(client, c["roaster_id"], c["name"], known, rule, min_coffees)
                        ),
                        chunk,
                    )
                )
            done = [(c, r) for c, r in zip(chunk, results, strict=True) if r is not None]
            totals["skipped_deadline"] += len(chunk) - len(done)
            _write(lake, run_date, done, version)
            totals["screened"] += len(done)
            totals["admitted"] += sum(r["admitted"] for _, r in done)
            lake.publish(f"screen {run_date} batch {i // CHUNK}")
    return totals


def _write(lake: Lake, run_date: str, done: list[tuple[dict, dict]], version: str) -> None:
    if not done:
        return
    now = datetime.now(UTC).replace(microsecond=0)
    rd = date.fromisoformat(run_date)
    batch = new_batch_id()
    lake.write_partition(
        "screens",
        run_date,
        batch,
        [
            {"run_date": rd, "batch_id": batch, "screened_at": now, "roaster_id": c["roaster_id"],
             "candidate_source": c["source"], "tag": c.get("tag"), "code_version": version,
             **{k: r[k] for k in SCREEN_FIELDS}}
            for c, r in done
        ],
    )  # fmt: skip
    admitted = [(c, r) for c, r in done if r["admitted"]]
    if not admitted:
        return
    lake.write(
        "registry",
        f"registry/source=screen/{run_date}-{batch}.parquet",
        [
            {"added_at": now, "source": f"screen-{c['source']}", "roaster_id": c["roaster_id"],
             "name": c["name"] or c["roaster_id"], "city": c.get("city"), "state": c.get("state"),
             "locations": c.get("locations"), "confidence": c.get("confidence"), "tagged_roastery": False,
             "roast_in_name": False, "opted_out": False}
            for c, _ in admitted
        ],
    )  # fmt: skip
    # The screen's probe is a probe: the crawl reads the platform from it, without probing again.
    lake.write_partition(
        "probes",
        run_date,
        batch,
        [
            {"run_date": rd, "batch_id": batch, "probed_at": now, "roaster_id": c["roaster_id"],
             "status": r["status"], "http_status": r["http_status"], "final_host": r["final_host"],
             "platform": r["platform"]}
            for c, r in admitted
        ],
    )  # fmt: skip


SCREEN_FIELDS = (
    "status", "http_status", "final_host", "platform", "products", "coffees", "named", "partial", "own_vendor",
    "other_roaster", "roast_phrase", "serves_phrase", "rule", "min_coffees", "admitted", "reason", "requests",
    "duration_s",
)  # fmt: skip
