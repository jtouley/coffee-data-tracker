-- Did this week's run try every roaster it should have? One row per roaster expected in the run (as_of_run, or
-- the latest run). The expected set is what the run's full ingests recorded, before fetching, that they set out
-- to fetch (run_manifests with mode 'full'), plus any roaster the run actually tried. A run with no full manifest
-- (one from before manifests, or only --sample, --only or --retry-failed ingests) has its set reconstructed:
-- every roaster that is ingestable now (pending, active, no_coffee, no_catalog) and was known before the run
-- started, plus any roaster the run tried. Any other manifest only adds to the set, never shrinks it, so a partial
-- ingest can't make a run look complete. Ingestion is append-only, so a skipped roaster leaves no trace except its
-- absence; this is where that absence becomes visible. A failed fetch counts as attempted.
--
-- has_catalog and catalog_ok are what run_ok_rate and adapter_ok_rate count. A roaster known to have no catalog
-- (no_catalog: its fetches succeed with 0 products) says nothing about whether the run worked, so it is left out:
-- has_catalog is false when this run's fetch succeeded with 0 products, or failed after only empty successes,
-- and no fetch up to this run ever returned a product. A roaster never fetched successfully is unknown, and counts
-- (a failure there counts against the run). catalog_ok: this run fetched it and got products, so an adapter that
-- "succeeds" with nothing doesn't count as working for a roaster that has a catalog. adapter: this run's latest
-- attempt's, else its latest earlier one's, else the one its platform maps to (models.Roaster.adapter).
with run as (
    {% if var('as_of_run') %}
    select cast('{{ var("as_of_run") }}' as date) as run_date
    {% else %}
    -- A run with a manifest and no fetch yet (killed before its first chunk) is still the latest run.
    select max(run_date) as run_date from (
        select run_date from {{ ref('int_roaster_runs') }}
        union all
        select run_date from {{ lake('run_manifests') }} where roaster_id is not null
    )
    {% endif %}
),
attempts as (select r.* from {{ ref('int_roaster_runs') }} r join run using (run_date)),
earlier as (
    select r.roaster_id, arg_max(r.adapter, r.run_date) as adapter, bool_or(r.ok) as any_ok,
           bool_or(r.ok and coalesce(r.products > 0, true)) as any_products
    from {{ ref('int_roaster_runs') }} r join run on r.run_date < run.run_date
    group by r.roaster_id
),
started as (select min(first_at) as started_at from attempts),
manifest as (
    select m.roaster_id, bool_or(m.mode = 'full') as full_run
    from {{ lake('run_manifests') }} m join run using (run_date)
    where m.roaster_id is not null
    group by m.roaster_id
),
reconstructed as (
    select r.roaster_id
    from {{ ref('dim_roaster') }} r
    left join {{ ref('stg_registry') }} a using (roaster_id)
    cross join started
    where r.status in ('pending', 'active', 'no_coffee', 'no_catalog')
      and (a.first_added is null or a.first_added < coalesce(started.started_at, now()))
      and not exists (select 1 from manifest where full_run)
),
expected as (
    select roaster_id from manifest
    union
    select roaster_id from reconstructed
    union
    select roaster_id from attempts
)
select
    run.run_date, e.roaster_id, t.roaster_id is not null as attempted, coalesce(t.ok, false) as ok,
    case when exists (select 1 from manifest where full_run) then 'manifest' else 'reconstructed' end
        as expected_from,
    coalesce(t.adapter, p.adapter, case
        when d.platform in ('shopify', 'woocommerce') then d.platform
        when rtrim(d.platform, '?') in ('squarespace', 'square_online') then rtrim(d.platform, '?')
        else 'generic' end) as adapter,
    case
        when coalesce(p.any_products, false) or coalesce(t.products > 0, false) then true
        when coalesce(t.ok, false) then t.products is null  -- succeeded with 0 products (null: an older file)
        else not coalesce(p.any_ok, false)  -- failed, or not tried, after only empty successes; or unknown
    end as has_catalog,
    coalesce(t.ok, false) and coalesce(t.products > 0, true) as catalog_ok
from expected e
cross join run
left join attempts t using (roaster_id)
left join earlier p using (roaster_id)
left join {{ ref('dim_roaster') }} d using (roaster_id)
