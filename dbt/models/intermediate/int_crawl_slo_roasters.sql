-- Report only (tag report_only): `coffeedata build` builds it after the gate, so it can never hold back a release.
{{ config(tags=['report_only']) }}
-- The crawl success SLO's population (#96, docs/TRIAGE.md "The crawl success SLO"): one row per scored run and
-- roaster known in it, whether it counts, and how the run left it. The owner's definition (2026-11-02): the
-- denominator is every roaster the run is expected to fetch, not opted out, with a platform, across every
-- connector; unreachable and no-catalog roasters count against it.
--
-- Everything is read from what the runs themselves stored (their manifests and their fetches), up to the run being
-- built (as_of_run, else the latest stored): no clock, no earlier build, and no roaster state from after the run.
-- So the same raw builds the same rows, and a run built again later has the rows it had. Two exceptions, on rows
-- the run didn't list (below).
--
-- Scored runs: the scheduled weekly runs only. The repo has no one flag for that, so this combines the two
-- definitions it has. The weekly run is dated the Monday of its week: the scheduled crawl jobs run on Mondays and
-- date their run with connectors.week_of. And it set out to fetch every roaster: one of its manifests has mode
-- 'full', the test run_completeness uses to take a run's expected set from its manifests; a run from before
-- manifests (none at all) is taken as full. So an ad-hoc ingest dated another day (`--run-date` defaults to today:
-- the stored runs of 2026-10-27, 10-28 and 10-30), and a Monday with only sample, --only or retry_failed
-- manifests, have no rows here and spend no budget. A week whose crawl never started has no stored run, so no row
-- and no budget spent either: freshness:served_age_days is what reports a missed week.
-- Known in a run: every roaster the run listed in a full manifest (the crawl's pool when it started: the roasters
-- with a platform, not opted out and not an alias), or, for a run from before manifests, tried; plus every roaster
-- an earlier scored run listed that this one didn't (expected_by_run false). Those are the roasters the crawl has
-- stopped trying (unreachable after max_failures failed runs): they count as not fetched, so giving up on a store
-- never raises the share. A partial pull (an --only, "Pull now", sample or retry_failed manifest, or an ad-hoc run)
-- never makes a roaster known: a roaster only such a pull touched has no row in that run or any later one. Nor
-- does the registry alone: a roaster no scored run has listed has no row, and one removed from the registry (no
-- int_roaster_state row now) has no row in the runs that didn't list it. That removal, like an alias, is read as
-- it is now (raw has no date for it).
--
-- counted is false, with excluded_reason, only for a roaster the run didn't list:
--   opted_out  it had opted out before the run started (stg_registry.opted_out_at; an opt-out stated in the seed
--              list has no date and counts from the start);
--   alias      another roaster_id serves the same store, which is counted once, as itself. The one attribute read
--              as it is now, not as it was at the run: raw records no date for it.
-- A roaster the run listed or tried counts, whatever became of it later. Everything else counts too: chains
-- (crawled, only left out of the public tables), no-catalog and no-coffee stores, stores found closed in the run,
-- quarantined, challenged, parked or blocked ones, and those not tried at all.
--
-- ok is the run's int_roaster_runs.ok, as run_completeness.ok: an attempt in the run was ok, so a retry that fixed
-- a failure counts, a partial read counts, and so does an ok fetch of a store with no products. outcome says why
-- not. platform is the connector as the runs recorded it: the run's latest attempt's adapter, else its manifest's,
-- else the latest earlier run's. status is the roaster's when the run's manifest listed it.
with anchor as (
    {% if var('as_of_run') %}
    select cast('{{ var("as_of_run") }}' as date) as run_date
    {% else %}
    select max(run_date) as run_date from (
        select run_date from {{ ref('int_roaster_runs') }}
        union all
        select run_date from {{ ref('stg_run_manifests') }}
    )
    {% endif %}
),
attempts as (
    select t.* from {{ ref('int_roaster_runs') }} t join anchor a on t.run_date <= a.run_date
),
-- A roaster can be in several of a run's manifests (a resume, a retry): the latest row's status and adapter, ties
-- by the manifest id and then the value, so the pick never depends on the order rows are read.
manifests as (
    select m.run_date, m.roaster_id, min(m.started_at) as started_at,
           bool_or(coalesce(m.mode = 'full', false)) as full_run,
           arg_max(m.status, (m.started_at, m.manifest_id, m.status)) as status,
           arg_max(m.adapter, (m.started_at, m.manifest_id, m.adapter)) as adapter
    from {{ ref('stg_run_manifests') }} m join anchor a on m.run_date <= a.run_date
    group by m.run_date, m.roaster_id
),
-- The roasters a run set out to fetch: its full manifests' rows; a run from before manifests, the ones it tried.
listed_ids as (
    select run_date, roaster_id from manifests where full_run
    union
    select t.run_date, t.roaster_id from attempts t
    where not exists (select 1 from manifests m where m.run_date = t.run_date)
),
listed as (
    select i.run_date, i.roaster_id, m.status, coalesce(t.adapter, m.adapter) as adapter,
           t.roaster_id is not null as attempted, coalesce(t.ok, false) as ok,
           coalesce(t.unavailable, false) as unavailable, coalesce(t.quarantined, false) as quarantined, t.challenge
    from listed_ids i
    left join manifests m on m.run_date = i.run_date and m.roaster_id = i.roaster_id
    left join attempts t on t.run_date = i.run_date and t.roaster_id = i.roaster_id
),
stored_runs as (
    select run_date, min(started_at) as started_at, bool_or(full_run) as has_full_manifest,
           bool_or(has_manifest) as has_manifest
    from (
        select run_date, first_at as started_at, false as full_run, false as has_manifest from attempts
        union all
        select run_date, started_at, full_run, true from manifests
    )
    group by run_date
),
scored_runs as (
    select run_date, started_at from stored_runs
    where isodow(run_date) = 1 and (has_full_manifest or not has_manifest)
),
first_listed as (
    select l.roaster_id, min(l.run_date) as run_date from listed l join scored_runs r using (run_date)
    group by l.roaster_id
),
connectors as (
    select l.run_date, l.roaster_id, l.adapter from listed l join scored_runs r using (run_date)
    where l.adapter is not null
),
judged as (
    select
        r.run_date, f.roaster_id, l.status, l.roaster_id is not null as expected_by_run,
        coalesce(l.adapter, p.adapter, 'unknown') as platform,
        case
            when l.roaster_id is not null then null
            when coalesce(s.opted_out, false) and (g.opted_out_at is null or g.opted_out_at < r.started_at)
                then 'opted_out'
            when coalesce(s.status = 'alias', false) then 'alias'
        end as excluded_reason,
        coalesce(l.attempted, false) as attempted,
        coalesce(l.ok, false) as ok,
        case
            when coalesce(l.ok, false) then 'ok'
            when coalesce(l.unavailable, false) then 'unavailable'
            when coalesce(l.quarantined, false) then 'quarantined'
            when l.challenge is not null then 'challenged'
            when coalesce(l.attempted, false) then 'failed'
            else 'not_attempted'
        end as outcome,
        r.run_date = a.run_date as is_build_run
    from scored_runs r
    cross join anchor a
    join first_listed f on f.run_date <= r.run_date
    left join listed l on l.run_date = r.run_date and l.roaster_id = f.roaster_id
    asof left join connectors p on p.roaster_id = f.roaster_id and p.run_date < r.run_date
    left join {{ ref('int_roaster_state') }} s on s.roaster_id = f.roaster_id
    left join {{ ref('stg_registry') }} g on g.roaster_id = f.roaster_id
    where l.roaster_id is not null or s.roaster_id is not null
)
select run_date, roaster_id, platform, status, excluded_reason is null as counted, excluded_reason,
       expected_by_run, attempted, ok, outcome, is_build_run
from judged
