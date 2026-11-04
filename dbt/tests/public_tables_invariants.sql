-- Rules the public tables hold by construction, so a row breaking one is a bug in the SQL that built it, never a
-- change in the market. One row per offending row, named by its rule:
--   public_market_weekly: p10 <= median <= p90 (all quantiles of the same prices), in_stock <= coffees and
--     roasters <= coffees (counts of a subset of the same rows);
--   public_coffee: first_seen <= last_seen (the first and last counted runs that listed it);
--   public_price_event: a price event's prices differ (price_event: price_usd is distinct from the previous), and a
--     new event has no previous price (it is the bag's first observation).
-- Not here, because the SQL doesn't guarantee them: last_seen <= release_run_date (dim_coffee reads every run in
-- the lake, and `build --run-date` can name an older run; public_coffee_is_fresh holds max(last_seen) to the run),
-- and a price event having both prices set (a bag whose price went missing, or appeared, is a price event: the
-- fct_price_event unit tests have one).
select 'market_quantiles_in_order' as rule, origin_country || '|' || process as row_key
from {{ ref('public_market_weekly') }}
where p10_price_per_100g > median_price_per_100g or median_price_per_100g > p90_price_per_100g
union all
select 'market_in_stock_within_coffees', origin_country || '|' || process
from {{ ref('public_market_weekly') }} where in_stock > coffees
union all
select 'market_roasters_within_coffees', origin_country || '|' || process
from {{ ref('public_market_weekly') }} where roasters > coffees
union all
select 'coffee_first_seen_before_last_seen', coffee_id from {{ ref('public_coffee') }} where first_seen > last_seen
union all
select 'price_event_prices_differ', coffee_id || '|' || variant_id || '|' || run_date
from {{ ref('public_price_event') }} where event = 'price' and price_usd is not distinct from previous_price_usd
union all
select 'new_event_has_no_previous_price', coffee_id || '|' || variant_id || '|' || run_date
from {{ ref('public_price_event') }} where event = 'new' and previous_price_usd is not null
