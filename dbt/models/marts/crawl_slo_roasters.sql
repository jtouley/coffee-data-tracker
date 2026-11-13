-- The crawl success SLO's population (#96, docs/TRIAGE.md "The crawl success SLO"): one row per stored run (up to
-- the run being built: as_of_run, else the latest) and roaster known when that run started, whether it counts, and
-- how the run left it. The owner's definition (2026-11-02): the denominator is every roaster the run is expected to
-- fetch, not opted out, with a platform, across every connector; unreachable and no-catalog roasters count against it.
--
-- Read from the stored runs alone (fetches and run manifests, the registry, and the roaster state they give): no
-- clock, and no earlier build's output, so the same raw builds the same rows, and building an older run again reads
-- nothing stored after it except a roaster's current state (below). Runs after as_of_run are left out.
--
-- Known in a run: every roaster the run listed (run_manifests, any mode) or tried (int_roaster_runs), plus every
-- roaster the registry listed before the run's first fetch (before the end of its run_date, for a run with a
-- manifest and no fetch). That contains run_completeness's expected set for the run, and adds what it leaves out: a
-- roaster the crawl has stopped trying (status unreachable after max_failures failed runs), and one a full manifest
-- didn't list. Giving up on a store must not raise the share.
--
-- counted is false, with excluded_reason (from the roaster's state as this build's raw gives it, dim_roaster):
--   opted_out    the owner or the roaster said not to fetch it, so no run is expected to;
--   alias        another roaster_id serves the same store (canonical_roaster_id), which is counted once, as itself;
--   no_platform  no connector can be named for it (never probed ok, or its probe answered an HTTP error), and the
--                run didn't list or try it. A roaster the run listed or tried counts whatever its platform says.
-- Everything else counts: chains (crawled, only left out of the public tables), no-catalog and no-coffee stores,
-- stores found closed in the run, quarantined, challenged, parked or blocked ones, and those not tried at all.
--
-- ok is the run's int_roaster_runs.ok, as run_completeness.ok: an attempt in the run was ok, so a retry that fixed
-- a failure counts, a partial read counts, and so does an ok fetch of a store with no products. outcome says why
-- not. platform is the connector, as run_completeness.adapter: the run's latest attempt's, else its latest
-- earlier run's, else the one the roaster's platform maps to.
with attempts as (select * from {{ ref('int_roaster_runs') }}),
manifest as (
    select distinct run_date, roaster_id from {{ lake('run_manifests') }} where roaster_id is not null
),
anchor as (
    {% if var('as_of_run') %}
    select cast('{{ var("as_of_run") }}' as date) as run_date
    {% else %}
    select max(run_date) as run_date from (
        select run_date from attempts
        union all
        select run_date from manifest
    )
    {% endif %}
),
runs as (
    select r.run_date, min(a.first_at) as started_at
    from (select run_date from attempts union select run_date from manifest) r
    join anchor on r.run_date <= anchor.run_date
    left join attempts a on a.run_date = r.run_date
    group by r.run_date
),
known as (
    select runs.run_date, d.roaster_id
    from {{ ref('dim_roaster') }} d
    left join {{ ref('stg_registry') }} g using (roaster_id)
    cross join runs
    where g.first_added is null
       or g.first_added < coalesce(runs.started_at, cast(runs.run_date + 1 as timestamp) at time zone 'UTC')
),
listed as (
    select run_date, roaster_id from manifest join runs using (run_date)
    union
    select run_date, roaster_id from attempts join runs using (run_date)
),
candidates as (
    select run_date, roaster_id, true as expected_by_run from listed
    union all
    select run_date, roaster_id, false from known anti join listed using (run_date, roaster_id)
),
judged as (
    select
        c.run_date, c.roaster_id, d.status, c.expected_by_run,
        coalesce(t.adapter, p.adapter, case
            when d.platform in ('shopify', 'woocommerce') then d.platform
            when rtrim(d.platform, '?') in ('squarespace', 'square_online') then rtrim(d.platform, '?')
            else 'generic' end) as platform,
        case
            when coalesce(d.status = 'opted_out', false) then 'opted_out'
            when coalesce(d.status = 'alias', false) then 'alias'
            when not c.expected_by_run and (d.platform is null or d.platform like 'http\_%' escape '\')
                then 'no_platform'
        end as excluded_reason,
        t.roaster_id is not null as attempted,
        coalesce(t.ok, false) as ok,
        case
            when coalesce(t.ok, false) then 'ok'
            when coalesce(t.unavailable, false) then 'unavailable'
            when coalesce(t.quarantined, false) then 'quarantined'
            when t.challenge is not null then 'challenged'
            when t.roaster_id is not null then 'failed'
            else 'not_attempted'
        end as outcome
    from candidates c
    left join attempts t on t.roaster_id = c.roaster_id and t.run_date = c.run_date
    asof left join attempts p on p.roaster_id = c.roaster_id and p.run_date < c.run_date
    left join {{ ref('dim_roaster') }} d on d.roaster_id = c.roaster_id
)
select run_date, roaster_id, platform, status, excluded_reason is null as counted, excluded_reason,
       expected_by_run, attempted, ok, outcome
from judged
