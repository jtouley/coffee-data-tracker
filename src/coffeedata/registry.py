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

ROOT = Path(__file__).resolve().parents[2]
# Hosts many unrelated domains resolve to, one list for dbt and the crawl: dbt's seed shared_hosts (kinds
# not_a_roaster, platform, parked, shortener, directory; a host covers its subdomains). Sharing one is no evidence of
# one store, so such a host never groups roasters (dbt/macros/roaster_alias.sql).
SHARED_HOSTS_CSV = ROOT / "dbt" / "seeds" / "shared_hosts.csv"


def _shared_hosts() -> list[dict]:
    with SHARED_HOSTS_CSV.open(newline="") as f:
        return list(csv.DictReader(f))


# Hosts that are never a roaster's own storefront (full suffixes): the seed's not_a_roaster rows (social, listing and
# ordering sites; its note column says why an entry is there).
NOT_A_ROASTER_SITE = tuple(r["host"] for r in _shared_hosts() if r["kind"] == "not_a_roaster")

MAX_FAILURES = 3


def normalize_domain(d: str) -> str:
    d = d.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0].strip(".")
    return re.sub(r"^(www\d?|locations|shop|store)\.", "", d)


def host_key(host: str) -> str:
    """One store's key from a resolved host: normalize_domain (www.Jonescoffeeroasters.com and jonescoffeeroasters.com
    are one store). dbt's host_key macro (dbt/macros/roaster_alias.sql) is the same rule in SQL; tests/test_registry.py
    checks the two agree."""
    return normalize_domain(host)


ALIAS_MACROS = ROOT / "dbt" / "macros" / "roaster_alias.sql"


def alias_macros():
    """dbt/macros/roaster_alias.sql as a jinja2 module (host_key, is_shared_host, is_publishable, roaster_canonical):
    the one statement of the alias rule, which dbt calls in int_roaster_state and STATE_SQL renders here."""
    import jinja2

    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    return env.from_string(ALIAS_MACROS.read_text()).module


def not_a_roaster_site(domain: str) -> bool:
    """The domain is, or is under, a NOT_A_ROASTER_SITE host."""
    return any(domain == s or domain.endswith("." + s) for s in NOT_A_ROASTER_SITE)


def is_storefront_host(domain: str) -> bool:
    """A domain that can be a roaster's own storefront: not a NOT_A_ROASTER_SITE host, and never a host only the
    scheduled importers job may fetch (http.schedule_only_host: Royal Coffee), so discovery and add-roaster refuse it."""
    from .http import schedule_only_host

    return "." in domain and not not_a_roaster_site(domain) and not schedule_only_host(domain)


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
           sources, nullif(country, '') as country, nullif(lower(trim(alias_of)), '') as alias_of
    from read_csv(?, header = true, all_varchar = true)
),
added as (
    -- ties broken as in dbt's stg_registry and dim_roaster, so the two always agree
    select roaster_id, arg_max(name, (added_at, name)) as name, arg_max(city, (added_at, city)) as city,
           arg_max(state, (added_at, state)) as state, max(locations) as locations, bool_or(opted_out) as opted_out,
           string_agg(distinct source, '+' order by source) as sources, min(added_at) as first_added
    from {registry} where roaster_id is not null group by roaster_id
),
-- Hosts many unrelated domains resolve to (dbt's seed shared_hosts); not_a_roaster ones are never a roaster.
shared_hosts as (select host, kind from read_csv('{shared_hosts}', header = true, all_varchar = true)),
roasters as (
    select coalesce(s.roaster_id, a.roaster_id) as roaster_id, coalesce(a.name, s.name) as name,
           coalesce(a.city, s.city) as city, coalesce(a.state, s.state) as state,
           coalesce(s.locations, a.locations, 1) as locations, coalesce(s.is_chain, false) as is_chain,
           coalesce(a.opted_out, false) or coalesce(s.opted_out, false) as opted_out,
           s.final_host as seed_host, s.platform as seed_platform, s.last_probed as seed_probed,
           concat_ws('+', s.sources, a.sources) as sources, coalesce(s.country, 'US') as country, s.alias_of,
           a.first_added
    from seed s full outer join added a using (roaster_id)
    -- Dropped before the state and the stores are decided, so a dropped roaster never groups, as alias or
    -- canonical: a platform or vendor site (NOT_A_ROASTER_SITE: an older seed or a lake registry row), and any
    -- roaster the caller drops (registry.current's `dropped`: the admin's "not a roaster" flags, which dbt drops in
    -- stg_registry). dbt keeps NOT_A_ROASTER_SITE ids in dim_roaster, but the alias rule never groups them either
    -- (macro roaster_canonical: a shared host), so both sides decide every other roaster's store alike.
    where not exists (
        select 1 from shared_hosts sh
        where sh.kind = 'not_a_roaster'
          and (coalesce(s.roaster_id, a.roaster_id) = sh.host or ends_with(coalesce(s.roaster_id, a.roaster_id), '.' || sh.host))
    ){dropped}
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
           max(products) filter (where status = 'ok') as products,
           row_number() over (partition by roaster_id order by run_date desc) as rn
    from fetches group by roaster_id, run_date
),
recent as (
    select roaster_id, count(*) filter (where rn <= {k}) as attempts,
           count(*) filter (where rn <= {k} and not ok) as errors, max(last_at) as last_fetch,
           -- the first run that read a catalog (history: it only ever moves earlier), for the canonical id
           min(run_date) filter (where ok and products > 0) as first_catalog_run
    from runs group by roaster_id
),
last_ok as (
    select * from fetches where status = 'ok'
    qualify row_number() over (partition by roaster_id order by fetched_at desc, batch_id desc) = 1
),
base as (
select
    r.roaster_id, r.name, r.city, r.state, r.locations, r.is_chain, r.opted_out, r.sources, r.country, r.alias_of,
    r.first_added, rc.first_catalog_run, coalesce(g.final_host, r.seed_host, r.roaster_id) as host,
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
-- One store, one roaster_id: dbt/macros/roaster_alias.sql, rendered (alias_macros), so dbt's int_roaster_state
-- decides the same. Any roaster but its store's canonical id is an alias: never crawled (ingest.crawlable), still
-- probed, so it becomes its own store again if a later ok probe resolves it elsewhere.
canonical as (
{canonical}
)
select
    b.roaster_id, b.name, b.city, b.state, b.locations, b.is_chain, b.opted_out, b.host, b.platform, b.last_probed,
    case when b.status <> 'opted_out' and c.canonical_roaster_id <> b.roaster_id then 'alias' else b.status end
        as status,
    c.canonical_roaster_id
from base b
join canonical c using (roaster_id)
"""


def state_sql(lake: Lake, dropped: frozenset[str] | set[str] = frozenset()) -> str:
    return STATE_SQL.format(
        registry=lake.scan("registry"),
        probes=lake.scan("probes"),
        fetches=lake.scan("fetches"),
        k=MAX_FAILURES,
        shared_hosts=_sql_str(str(SHARED_HOSTS_CSV)),
        dropped=(
            "\n    and coalesce(s.roaster_id, a.roaster_id) not in ("
            + ", ".join(f"'{_sql_str(d)}'" for d in sorted(dropped))
            + ")"
            if dropped
            else ""
        ),
        canonical=alias_macros().roaster_canonical("base", "shared_hosts"),
    )


def _sql_str(v: str) -> str:
    return v.replace("'", "''")


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


def same_store(probed: list[str], before: list[RoasterState], after: list[RoasterState]) -> dict[str, str]:
    """The probe's report of a domain that only adds a store twice: each probed roaster that is an alias in the state
    after the probe (registry.current, so the crawl's own rule) and wasn't one of the same store before, mapped to its
    canonical id. Such a domain (often an old or a cafe domain that redirects to the shop) is never crawled or
    published; the probe job records the list (detail `same_store`) so the owner sees what discovery tried to add. An
    alias probed again and still an alias isn't listed again."""
    was = {s.roaster.roaster_id: s.canonical_roaster_id for s in before if s.status == "alias"}
    now = {s.roaster.roaster_id: s for s in after}
    out = {}
    for rid in probed:
        s = now.get(rid)
        if (
            s is not None
            and s.status == "alias"
            and s.canonical_roaster_id
            and was.get(rid) != s.canonical_roaster_id
        ):
            out[rid] = s.canonical_roaster_id
    return dict(sorted(out.items()))


@dataclass
class RoasterState:
    roaster: Roaster
    status: str
    last_probed: str | None
    # the roaster_id its store is crawled and published under (itself unless status is alias); None where unknown
    canonical_roaster_id: str | None = None


def current(
    lake: Lake, seed: Path = SEED, dropped: frozenset[str] | set[str] = frozenset()
) -> list[RoasterState]:
    """Every roaster's state now. Retried on a transient connection error to a remote lake (lake.retry_transient):
    it is each crawl job's first read of the hf:// lake. `dropped`: roaster_ids that are not roasters (the admin's
    flags), left out before the stores are decided, as NOT_A_ROASTER_SITE hosts are."""
    return retry_transient(_current, lake, seed, dropped)


def _current(lake: Lake, seed: Path, dropped: frozenset[str] | set[str] = frozenset()) -> list[RoasterState]:
    con = connect_for_state(lake)
    cur = con.execute(state_sql(lake, dropped) + " order by roaster_id", [str(seed)])
    cols = [d[0] for d in cur.description]
    out = []
    from .http import schedule_only_host

    for row in cur.fetchall():
        r = dict(zip(cols, row, strict=True))
        # a host only the scheduled importers job may fetch is never a roaster, from the seed or a lake addition
        if schedule_only_host(r["roaster_id"]) or schedule_only_host(r["host"]):
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
        out.append(
            RoasterState(
                roaster,
                r["status"],
                str(r["last_probed"]) if r["last_probed"] else None,
                r["canonical_roaster_id"],
            )
        )
    return out
