-- Which batch counts for each roaster-week with a successful fetch, and whether the week counts at all.
-- A "successful" fetch can still be wrong (a store that cut its catalog short, an adapter bug), and in history
-- that reads as half a roaster's coffees being removed. So a suspect week (suspect_run) is held out of the
-- history, like an outage, until someone accepts it (coffeedata override). The baseline only ever learns from
-- counted weeks, so a store that stays broken stays flagged instead of becoming its own new normal. Raw batches are
-- never touched; this only decides which batch counts. stg_run_quality reports the result.
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
    select roaster_id, rn, []::bigint[] as prior, counted,
        case when counted then [products] else []::bigint[] end as history
    from (
        -- A first week has no baseline, so it is never suspect: it counts unless rejected. (A use_batch whose
        -- batch wasn't picked is no override, here as in later weeks.)
        select *, coalesce(override_action, '') <> 'reject' as counted
        from seq where rn = 1
    )
    union all
    select roaster_id, rn, prior, counted,
        case when counted then list_concat(prior, [products])[-4:] else prior end as history
    from (
        select *,
            case
                when override_action = 'reject' then false
                when override_action = 'accept' or (override_action = 'use_batch' and pinned) then true
                else not suspect  -- a use_batch whose batch doesn't exist or failed is no override at all
            end as counted
        from (
            select s.*, w.history as prior, {{ suspect_run('s.products', 'w.history') }} as suspect
            from walk w join seq s on s.roaster_id = w.roaster_id and s.rn = w.rn + 1
        )
    )
)
-- history: the product counts of the counted weeks before this one, the baseline it was judged against.
select
    s.roaster_id, s.run_date, s.batch_id, s.fetched_at, s.products, s.coffees, w.prior as history,
    s.override_action, s.override_reason, w.counted
from seq s
join walk w using (roaster_id, rn)
