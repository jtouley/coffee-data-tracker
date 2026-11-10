"""Which roasters exist (seed list + lake additions) and what state each is in (derived from history).

`config/roasters.csv` is the curated seed: one row per roaster website, keyed by its normalized domain
(`roaster_id`). Its `platform`/`final_host`/`last_probed` columns are the probe results from when the seed was
built; any probe recorded in the lake supersedes them. Nothing here is mutated by a pipeline run.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from . import classify, products
from .lake import Lake, retry_transient
from .models import Roaster

SEED = Path("config/roasters.csv")
SEED_FIELDS = [
    "roaster_id",
    "name",
    "city",
    "state",
    "sources",
    "locations",
    "confidence",
    "tagged_roastery",
    "roast_in_name",
    "is_chain",
    "opted_out",
    "final_host",
    "platform",
    "last_probed",
    "country",  # ISO 3166-1 alpha-2 (US unless a hand-checked correction says otherwise); dbt publishes US only
    "alias_of",  # hand-checked: the roaster_id that serves this same store (a rebrand that doesn't redirect)
]

# Hosts that are never a roaster's own storefront (full suffixes).
NOT_A_ROASTER_SITE = (
    "facebook.com",
    "instagram.com",
    "yelp.com",
    "linktr.ee",
    "toasttab.com",
    "google.com",
    "business.site",
    "square.site",
    "squareup.com",
    "order.online",
    "doordash.com",
    "ubereats.com",
    "grubhub.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "tripadvisor.com",
    "mapquest.com",
    "youtube.com",
    "clover.com",
    "wixsite.com",
    "godaddysites.com",
    "weebly.com",
    "blogspot.com",
    "wordpress.com",
    "gmail.com",
    # Ordering vendors that Overture lists as a cafe's website (crawl 2026-11-02): Per Diem's own site, for Java
    # House's North Liberty location (Java House is thejavahouse.com), and Spoon's ordering marketplace.
    "tryperdiem.com",
    "orderspoon.com",
)

MAX_FAILURES = 3


def normalize_domain(d: str) -> str:
    d = d.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0].strip(".")
    return re.sub(r"^(www\d?|locations|shop|store)\.", "", d)


def host_key(host: str) -> str:
    """One store's key from a resolved host: normalize_domain (www.Jonescoffeeroasters.com and jonescoffeeroasters.com
    are one store). HOST_KEY_SQL (and dbt's host_key macro) is the same rule in SQL."""
    return normalize_domain(host)


# host_key in SQL, for STATE_SQL; dbt/macros/roaster_alias.sql is the same expression.
HOST_KEY_SQL = (
    r"regexp_replace(rtrim(split_part(lower({h}), ':', 1), '.'), '^(www\d?|locations|shop|store)\.', '')"
)


def not_a_roaster_site(domain: str) -> bool:
    """The domain is, or is under, a NOT_A_ROASTER_SITE host."""
    return any(domain == s or domain.endswith("." + s) for s in NOT_A_ROASTER_SITE)


def is_storefront_host(domain: str) -> bool:
    return "." in domain and not not_a_roaster_site(domain)


def load_seed(path: Path = SEED) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# --- current state -----------------------------------------------------------------------------------

STATE_SQL = r"""
with seed as (
    select roaster_id, name, coalesce(nullif(city, ''), null) as city, nullif(state, '') as state,
           coalesce(try_cast(locations as integer), 1) as locations, coalesce(is_chain, 'false') = 'true' as is_chain,
           coalesce(opted_out, 'false') = 'true' as opted_out, nullif(final_host, '') as final_host,
           nullif(platform, '') as platform, try_cast(nullif(last_probed, '') as date) as last_probed,
           coalesce(sources, '') as sources, nullif(lower(trim(alias_of)), '') as alias_of
    from read_csv(?, header = true, all_varchar = true)
),
added as (
    -- ties broken as in dbt's stg_registry and dim_roaster, so the two always agree
    select roaster_id, arg_max(name, (added_at, name)) as name, arg_max(city, (added_at, city)) as city,
           arg_max(state, (added_at, state)) as state, max(locations) as locations, bool_or(opted_out) as opted_out,
           min(added_at) as first_added
    from {registry} where roaster_id is not null group by roaster_id
),
roasters as (
    select coalesce(s.roaster_id, a.roaster_id) as roaster_id, coalesce(a.name, s.name) as name,
           coalesce(a.city, s.city) as city, coalesce(a.state, s.state) as state,
           coalesce(s.locations, a.locations, 1) as locations, coalesce(s.is_chain, false) as is_chain,
           coalesce(a.opted_out, false) or coalesce(s.opted_out, false) as opted_out,
           s.final_host as seed_host, s.platform as seed_platform, s.last_probed as seed_probed,
           s.sources, s.alias_of, a.first_added
    from seed s full outer join added a using (roaster_id)
),
-- A probe whose robots.txt didn't answer is no answer about the site: it doesn't count as probed, so the next
-- probe run (stale-probe selection reads last_probed) tries the roaster again.
probes as (select * from {probes} where roaster_id is not null and status <> 'robots_unavailable'),
last_probe as (
    select * from probes
    qualify row_number() over (partition by roaster_id order by probed_at desc, status, final_host, platform) = 1
),
last_good_probe as (
    select * from probes where status = 'ok'
    qualify row_number() over (partition by roaster_id order by probed_at desc, final_host, platform) = 1
),
fetches as (select * from {fetches} where roaster_id is not null),
-- Failure streaks count runs, not attempts: a run is ok if any attempt in it succeeded.
runs as (
    select roaster_id, run_date, bool_or(status = 'ok') as ok, max(fetched_at) as last_at,
           row_number() over (partition by roaster_id order by run_date desc) as rn
    from fetches group by roaster_id, run_date
),
recent as (
    select roaster_id, count(*) filter (where rn <= {k}) as attempts,
           count(*) filter (where rn <= {k} and not ok) as errors, max(last_at) as last_fetch
    from runs group by roaster_id
),
last_ok as (
    select * from fetches where status = 'ok'
    qualify row_number() over (partition by roaster_id order by fetched_at desc, batch_id desc) = 1
),
base as (
select
    r.roaster_id, r.name, r.city, r.state, r.locations, r.is_chain, r.opted_out, r.sources, r.alias_of,
    r.first_added, coalesce(g.final_host, r.seed_host, r.roaster_id) as host,
    -- A failed probe never erases a platform we already know; only fetch history decides unreachability.
    coalesce(g.platform, r.seed_platform) as platform,
    coalesce(cast(lp.probed_at as date), r.seed_probed) as last_probed,
    case
        when r.opted_out then 'opted_out'
        when rc.attempts >= {k} and rc.errors = rc.attempts
             and (lp.probed_at is null or lp.probed_at < rc.last_fetch or lp.status <> 'ok') then 'unreachable'
        when coalesce(g.platform, r.seed_platform) is null
             or coalesce(g.platform, r.seed_platform) like 'http\_%' escape '\' then 'unreachable'
        when lo.roaster_id is null then 'pending'
        when coalesce(lo.coffees, lc.coffees, 0) > 0 then 'active'  -- as recorded by the crawl, else counted
        when lo.products > 0 then 'no_coffee'
        else 'no_catalog'
    end as status
from roasters r
left join last_probe lp using (roaster_id)
left join last_good_probe g using (roaster_id)
left join recent rc using (roaster_id)
left join last_ok lo using (roaster_id)
left join last_ok_coffees lc using (roaster_id)  -- registered by connect_for_state
),
-- One store, one roaster_id, as dbt's dim_roaster decides it (its comment has the reasons): among active and pending
-- roasters with the same host key, the canonical id is one already read, then the id equal to the key, then one
-- whose first label is the key's, then a curated one (the seed's sources: only the seed lists curated roasters),
-- the earliest added, the first by name; a hand-checked alias_of overrides the host. Any other is an alias: never
-- crawled (not in ingest.INGESTABLE), still probed, so it becomes its own store again if its redirect ends.
host_groups as (
    select roaster_id,
           first_value(roaster_id) over (
               partition by {hk}
               order by status = 'active' desc, roaster_id = {hk} desc,
                        split_part(roaster_id, '.', 1) = split_part({hk}, '.', 1) desc,
                        regexp_matches(coalesce(sources, ''), '(^|\+)curated') desc, first_added asc nulls last,
                        roaster_id
           ) as host_canonical
    from base
    where status in ('active', 'pending') and alias_of is null
)
select
    b.roaster_id, b.name, b.city, b.state, b.locations, b.is_chain, b.opted_out, b.host, b.platform, b.last_probed,
    case when b.status <> 'opted_out' and coalesce(b.alias_of, h.host_canonical, b.roaster_id) <> b.roaster_id
         then 'alias' else b.status end as status,
    coalesce(b.alias_of, h.host_canonical, b.roaster_id) as canonical_roaster_id
from base b
left join host_groups h using (roaster_id)
"""


def state_sql(lake: Lake) -> str:
    return STATE_SQL.format(
        registry=lake.scan("registry"),
        probes=lake.scan("probes"),
        fetches=lake.scan("fetches"),
        k=MAX_FAILURES,
        hk=HOST_KEY_SQL.format(h="host"),
    )


# The products of each roaster's last ok fetch whose crawl recorded no coffee count (fetches.coffees is null: an ELT
# crawl doesn't classify). Older fetches keep the count their crawl recorded.
LAST_OK_PRODUCTS_SQL = r"""
with last_ok as (
    select roaster_id, batch_id, coffees from {fetches} where roaster_id is not null and status = 'ok'
    qualify row_number() over (partition by roaster_id order by fetched_at desc) = 1
)
select distinct on (s.roaster_id, s.product_id) s.roaster_id, s.product_id, s.title, s.product_type, s.tags,
       list_transform(s.variants, lambda v: v.grams) as grams, s.body_hash, b.body_text
from {snapshots} s
join last_ok lo on s.roaster_id = lo.roaster_id and s.batch_id = lo.batch_id and lo.coffees is null
left join (select body_hash, any_value(body_text) as body_text from {bodies} group by 1) b using (body_hash)
"""


def connect_for_state(lake: Lake):
    """A connection with `last_ok_coffees` (roaster_id, coffees) registered for STATE_SQL: the coffees in each
    roaster's last ok fetch, counted as the crawl used to count them (classify.is_coffee, the rules alone, no
    verdicts), for a fetch whose crawl recorded no count (ELT). dbt's int_fetch_coffees counts the same way."""
    from collections import Counter

    import pyarrow as pa

    con = lake.connect()
    sql = LAST_OK_PRODUCTS_SQL.format(
        fetches=lake.scan("fetches"), snapshots=lake.scan("snapshots"), bodies=lake.scan("bodies")
    )
    n: Counter = Counter()
    for rid, _pid, title, ptype, tags, grams, bh, body in con.execute(sql).fetchall():
        n[rid] += classify.is_coffee(products.item(title, ptype, tags, grams, bh, body))
    schema = pa.schema([("roaster_id", pa.string()), ("coffees", pa.int64())])
    con.register(
        "last_ok_coffees", pa.table({"roaster_id": list(n), "coffees": list(n.values())}, schema=schema)
    )
    return con


def same_store(probed: list[dict], states: list[RoasterState]) -> dict[str, str]:
    """The probe's check for a domain that only adds a store twice: each probed roaster whose resolved host has the
    key of a different active roaster's host, mapped to that roaster. Such a domain (a new one from discovery, often an
    old or a café domain that redirects to the shop) is an alias from now on (STATE_SQL), so it is never crawled or
    published; the probe job records the list (detail `same_store`) so the owner sees what discovery tried to add."""
    active: dict[str, str] = {}
    for s in sorted(states, key=lambda s: s.roaster.roaster_id):
        if s.status == "active" and s.roaster.host:
            active.setdefault(host_key(s.roaster.host), s.roaster.roaster_id)
    out = {}
    for row in probed:
        rid, host = row.get("roaster_id"), row.get("final_host")
        if rid and host and row.get("status") == "ok":
            other = active.get(host_key(host))
            if other and other != rid:
                out[rid] = other
    return dict(sorted(out.items()))


@dataclass
class RoasterState:
    roaster: Roaster
    status: str
    last_probed: str | None


def current(lake: Lake, seed: Path = SEED) -> list[RoasterState]:
    """Every roaster's state now. Retried on a transient connection error to a remote lake (lake.retry_transient):
    it is each crawl job's first read of the hf:// lake."""
    return retry_transient(_current, lake, seed)


def _current(lake: Lake, seed: Path) -> list[RoasterState]:
    con = connect_for_state(lake)
    cur = con.execute(state_sql(lake) + " order by roaster_id", [str(seed)])
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        r = dict(zip(cols, row, strict=True))
        if not_a_roaster_site(r["roaster_id"]):
            # a platform or vendor site (NOT_A_ROASTER_SITE), from an older seed or a lake registry row: never a
            # roaster, so never crawled, whatever the lake still holds for it
            continue
        roaster = Roaster(
            roaster_id=r["roaster_id"],
            name=r["name"] or r["roaster_id"],
            city=r["city"] or "",
            state=r["state"] or "",
            host=r["host"],
            platform=r["platform"] or "",
            is_chain=bool(r["is_chain"]),
        )
        out.append(RoasterState(roaster, r["status"], str(r["last_probed"]) if r["last_probed"] else None))
    return out
