-- Changes between consecutive *successful* fetches of a roaster, per bag size. Derived entirely from the raw
-- snapshots, so a failed fetch can never look like a delisting and re-running a week changes nothing.
with obs as (
    select s.coffee_id, s.roaster_id, s.run_date, s.fetched_at, v.variant_id, v.price_usd,
           coalesce(v.available, true) as available
    from {{ ref('stg_snapshots') }} s
    join {{ ref('stg_products') }} p using (input_hash), unnest(s.variants) as t(v)
    where p.is_coffee
),
o as (
    select *,
        lag(run_date) over w as prev_obs_run, lag(price_usd) over w as prev_price,
        lag(available) over w as prev_available, lead(run_date) over w as next_obs_run
    from obs
    window w as (partition by coffee_id, variant_id order by run_date)
),
j as (
    select o.*, r.prev_complete_run, r.next_complete_run
    from o join {{ ref('stg_runs') }} r using (roaster_id, run_date)
),
events as (
    select run_date, fetched_at, coffee_id, roaster_id, variant_id, {{ price_event() }} as event,
        price_usd, prev_price as previous_price_usd, available
    from j
    union all
    -- Gone from the roaster's next complete successful fetch: removed as of that run. A partial run in between
    -- (a catalog read only in part) can't show a bag is gone, so it never removes one (stg_runs.next_complete_run;
    -- without partial runs it is next_run).
    select r.run_date, r.fetched_at, j.coffee_id, j.roaster_id, j.variant_id, 'removed', null, j.price_usd, false
    from j
    join {{ ref('stg_runs') }} r on r.roaster_id = j.roaster_id and r.run_date = j.next_complete_run
    where j.next_complete_run is not null and (j.next_obs_run is null or j.next_obs_run > j.next_complete_run)
)
select *, {{ release_columns() }} from events where event is not null
