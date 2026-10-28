-- Did this week's run try every roaster it should have? One row per roaster expected in the run (as_of_run, or
-- the latest run): every roaster that is ingestable (pending, active, no_coffee, no_catalog) and was known before
-- the run started, plus any roaster the run actually tried. Ingestion is append-only, so a skipped roaster leaves
-- no trace except its absence; this is where that absence becomes visible. A failed fetch counts as attempted.
with run as (
    {% if var('as_of_run') %}
    select cast('{{ var("as_of_run") }}' as date) as run_date
    {% else %}
    select max(run_date) as run_date from {{ ref('stg_fetches') }}
    {% endif %}
),
this_run as (select f.* from {{ ref('stg_fetches') }} f join run using (run_date)),
started as (select min(fetched_at) as started_at from this_run),
added as (
    select roaster_id, min(added_at) as first_added
    from {{ lake('registry') }} where roaster_id is not null group by roaster_id
),
attempts as (select roaster_id, bool_or(status = 'ok') as ok from this_run group by roaster_id),
expected as (
    select r.roaster_id
    from {{ ref('dim_roaster') }} r
    left join added a using (roaster_id)
    cross join started
    where r.status in ('pending', 'active', 'no_coffee', 'no_catalog')
      and (a.first_added is null or a.first_added < coalesce(started.started_at, now()))
    union
    select roaster_id from attempts
)
select
    run.run_date, e.roaster_id, t.roaster_id is not null as attempted, coalesce(t.ok, false) as ok
from expected e
cross join run
left join attempts t using (roaster_id)
