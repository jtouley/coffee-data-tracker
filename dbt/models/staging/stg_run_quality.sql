-- Every roaster-week with a successful fetch: the batch that counts for it, and whether its catalog looks broken.
-- A "successful" fetch can still be wrong (a store that cut its catalog short, an adapter bug), and in history
-- that reads as half a roaster's coffees being removed. So a week whose product count falls below
-- suspect_drop_ratio of the median of the roaster's last 4 *counted* weeks is held out of the history, like an
-- outage, until someone accepts it (coffeedata override). The baseline only ever learns from counted weeks, so a
-- store that stays broken stays flagged instead of becoming its own new normal. Raw batches are never touched;
-- this only decides which batch counts.
with recursive ok as (
    select roaster_id, run_date, batch_id, fetched_at, products, coffees
    from {{ ref('stg_fetches') }}
    where status = 'ok'
),
o as (select * from {{ ref('stg_run_overrides') }}),
-- The latest successful batch of the week, unless an override pins another of that week's batches.
picked as (
    select ok.*, coalesce(o.action = 'use_batch' and ok.batch_id = o.batch_id, false) as pinned
    from ok left join o using (roaster_id, run_date)
    qualify row_number() over (
        partition by ok.roaster_id, ok.run_date order by pinned desc, ok.fetched_at desc
    ) = 1
),
seq as (
    select p.*, o.action as override_action, o.reason as override_reason,
        row_number() over (partition by p.roaster_id order by p.run_date) as rn
    from picked p left join o using (roaster_id, run_date)
),
-- Walk each roaster's weeks in order, carrying the product counts of its last 4 counted weeks.
walk as (
    select roaster_id, rn, null::double as baseline_products, false as suspect, counted,
        case when counted then [products] else []::bigint[] end as history
    from (
        select *, coalesce(override_action, '') <> 'reject'
            and not coalesce(override_action = 'use_batch' and not pinned, false) as counted
        from seq where rn = 1
    )
    union all
    select roaster_id, rn, baseline_products, suspect, counted,
        case when counted then list_concat(history, [products])[-4:] else history end as history
    from (
        select *,
            case
                when override_action = 'reject' then false
                when override_action = 'accept' or (override_action = 'use_batch' and pinned) then true
                else not suspect  -- a use_batch whose batch doesn't exist or failed is no override at all
            end as counted
        from (
            select s.*, w.history,
                list_aggregate(w.history, 'median') as baseline_products,
                coalesce(
                    list_aggregate(w.history, 'median') >= {{ var('suspect_min_products') }}
                    and s.products < {{ var('suspect_drop_ratio') }} * list_aggregate(w.history, 'median'),
                    false
                ) as suspect
            from walk w join seq s on s.roaster_id = w.roaster_id and s.rn = w.rn + 1
        )
    )
)
select
    s.roaster_id, s.run_date, s.batch_id, s.fetched_at, s.products, s.coffees, w.baseline_products, w.suspect,
    s.override_action, s.override_reason, w.counted
from seq s
join walk w using (roaster_id, rn)
