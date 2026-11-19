-- One row per (week, coffee_id): click-outs to a coffee's page in its roaster's store, and the median position it
-- was clicked at (null when every click came from off a result list). Bots left out. No place, no query.
select week, coffee_id, min(roaster_id) as roaster_id, count(*) as click_outs,
    quantile_cont(rank, 0.5) as median_rank
from {{ ref('int_usage_events') }}
where event = 'click_out' and coffee_id is not null and not bot
group by week, coffee_id
