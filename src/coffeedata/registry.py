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

from .lake import Lake
from .models import Roaster

SEED = Path("config/roasters.csv")
SEED_FIELDS = ["roaster_id", "name", "city", "state", "sources", "locations", "confidence", "tagged_roastery",
               "roast_in_name", "is_chain", "opted_out", "final_host", "platform", "last_probed"]

# Hosts that are never a roaster's own storefront (full suffixes).
NOT_A_ROASTER_SITE = (
    "facebook.com", "instagram.com", "yelp.com", "linktr.ee", "toasttab.com", "google.com", "business.site",
    "square.site", "squareup.com", "order.online", "doordash.com", "ubereats.com", "grubhub.com", "tiktok.com",
    "twitter.com", "x.com", "linkedin.com", "tripadvisor.com", "mapquest.com", "youtube.com", "clover.com",
    "wixsite.com", "godaddysites.com", "weebly.com", "blogspot.com", "wordpress.com", "gmail.com",
)

MAX_FAILURES = 3


def normalize_domain(d: str) -> str:
    d = d.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0].strip(".")
    return re.sub(r"^(www\d?|locations|shop|store)\.", "", d)


def is_storefront_host(domain: str) -> bool:
    return "." in domain and not any(domain == s or domain.endswith("." + s) for s in NOT_A_ROASTER_SITE)


def load_seed(path: Path = SEED) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# --- current state -----------------------------------------------------------------------------------

STATE_SQL = r"""
with seed as (
    select roaster_id, name, coalesce(nullif(city, ''), null) as city, nullif(state, '') as state,
           coalesce(try_cast(locations as integer), 1) as locations, coalesce(is_chain, 'false') = 'true' as is_chain,
           coalesce(opted_out, 'false') = 'true' as opted_out, nullif(final_host, '') as final_host,
           nullif(platform, '') as platform, try_cast(nullif(last_probed, '') as date) as last_probed
    from read_csv(?, header = true, all_varchar = true)
),
added as (
    select roaster_id, arg_max(name, added_at) as name, arg_max(city, added_at) as city,
           arg_max(state, added_at) as state, max(locations) as locations, bool_or(opted_out) as opted_out
    from {registry} where roaster_id is not null group by roaster_id
),
roasters as (
    select coalesce(s.roaster_id, a.roaster_id) as roaster_id, coalesce(a.name, s.name) as name,
           coalesce(a.city, s.city) as city, coalesce(a.state, s.state) as state,
           coalesce(s.locations, a.locations, 1) as locations, coalesce(s.is_chain, false) as is_chain,
           coalesce(a.opted_out, false) or coalesce(s.opted_out, false) as opted_out,
           s.final_host as seed_host, s.platform as seed_platform, s.last_probed as seed_probed
    from seed s full outer join added a using (roaster_id)
),
probes as (select * from {probes} where roaster_id is not null),
last_probe as (select * from probes qualify row_number() over (partition by roaster_id order by probed_at desc) = 1),
last_good_probe as (
    select * from probes where status = 'ok'
    qualify row_number() over (partition by roaster_id order by probed_at desc) = 1
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
last_ok as (select * from fetches where status = 'ok' qualify row_number() over (partition by roaster_id order by fetched_at desc) = 1)
select
    r.roaster_id, r.name, r.city, r.state, r.locations, r.is_chain, r.opted_out,
    coalesce(g.final_host, r.seed_host, r.roaster_id) as host,
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
        when lo.coffees > 0 then 'active'
        when lo.products > 0 then 'no_coffee'
        else 'no_catalog'
    end as status
from roasters r
left join last_probe lp using (roaster_id)
left join last_good_probe g using (roaster_id)
left join recent rc using (roaster_id)
left join last_ok lo using (roaster_id)
"""


def state_sql(lake: Lake) -> str:
    return STATE_SQL.format(registry=lake.scan("registry"), probes=lake.scan("probes"),
                            fetches=lake.scan("fetches"), k=MAX_FAILURES)


@dataclass
class RoasterState:
    roaster: Roaster
    status: str
    last_probed: str | None


def current(lake: Lake, seed: Path = SEED) -> list[RoasterState]:
    con = lake.connect()
    cur = con.execute(state_sql(lake) + " order by roaster_id", [str(seed)])
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        r = dict(zip(cols, row, strict=True))
        roaster = Roaster(roaster_id=r["roaster_id"], name=r["name"] or r["roaster_id"], city=r["city"] or "",
                          state=r["state"] or "", host=r["host"], platform=r["platform"] or "",
                          is_chain=bool(r["is_chain"]))
        out.append(RoasterState(roaster, r["status"], str(r["last_probed"]) if r["last_probed"] else None))
    return out
