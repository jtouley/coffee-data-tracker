-- Every roaster-week with a successful fetch: the batch that counts for it, and whether its catalog looks broken.
-- A "successful" fetch can still be wrong (a store that cut its catalog short, an adapter bug), and in history
-- that reads as half a roaster's coffees being removed. So a week whose product count falls below
-- suspect_drop_ratio of the median of the roaster's previous 4 weeks is held out of the history, like an outage,
-- until someone accepts it (coffeedata override). Raw batches are never touched; this only decides which counts.
with ok as (
    select roaster_id, run_date, batch_id, fetched_at, products, coffees
    from {{ ref('stg_fetches') }}
    where status = 'ok'
),
o as (select * from {{ ref('stg_run_overrides') }}),
-- The latest successful batch of the week, unless an override pins another of that week's batches.
picked as (
    select ok.*
    from ok left join o using (roaster_id, run_date)
    qualify row_number() over (
        partition by ok.roaster_id, ok.run_date
        order by coalesce(o.action = 'use_batch' and ok.batch_id = o.batch_id, false) desc, ok.fetched_at desc
    ) = 1
),
baseline as (
    select *,
        median(products) over (
            partition by roaster_id order by run_date rows between 4 preceding and 1 preceding
        ) as baseline_products
    from picked
),
flagged as (
    select *,
        coalesce(
            baseline_products >= {{ var('suspect_min_products') }}
            and products < {{ var('suspect_drop_ratio') }} * baseline_products,
            false
        ) as suspect
    from baseline
)
select
    f.roaster_id, f.run_date, f.batch_id, f.fetched_at, f.products, f.coffees, f.baseline_products, f.suspect,
    o.action as override_action, o.reason as override_reason,
    case
        when o.action = 'reject' then false
        when o.action in ('accept', 'use_batch') then true
        else not f.suspect
    end as counted
from flagged f
left join o using (roaster_id, run_date)
