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
j as (select o.*, r.prev_run, r.next_run from o join {{ ref('stg_runs') }} r using (roaster_id, run_date)),
events as (
    select run_date, fetched_at, coffee_id, roaster_id, variant_id,
        case
            when prev_obs_run is null then 'new'
            when prev_obs_run is distinct from prev_run then 'relisted'
            when price_usd is distinct from prev_price then 'price'
            when available is distinct from prev_available then 'availability'
        end as event,
        price_usd, prev_price as previous_price_usd, available
    from j
    union all
    -- Gone from the roaster's next successful fetch: removed as of that run.
    select r.run_date, r.fetched_at, j.coffee_id, j.roaster_id, j.variant_id, 'removed', null, j.price_usd, false
    from j
    join {{ ref('stg_runs') }} r on r.roaster_id = j.roaster_id and r.run_date = j.next_run
    where j.next_run is not null and (j.next_obs_run is null or j.next_obs_run > j.next_run)
)
select * from events where event is not null
