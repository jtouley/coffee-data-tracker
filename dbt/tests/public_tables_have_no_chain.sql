-- No public table holds a chain's rows (dim_roaster.is_chain: more than chain_store_threshold stores, or an override
-- in seeds/chain_overrides.csv). public_roaster leaves chains out and every other public table follows it, so a row
-- here is a public table that stopped following it. One row per offending row. public_market_weekly has no roaster
-- column: it is counted from public_coffee alone.
with chains as (select roaster_id from {{ ref('dim_roaster') }} where is_chain)
select 'public_roaster' as public_table, roaster_id, roaster_id as row_key
from {{ ref('public_roaster') }} where roaster_id in (select roaster_id from chains)
union all
select 'public_coffee', roaster_id, coffee_id
from {{ ref('public_coffee') }} where roaster_id in (select roaster_id from chains)
union all
select 'public_price_event', roaster_id, coffee_id || '|' || variant_id || '|' || run_date
from {{ ref('public_price_event') }} where roaster_id in (select roaster_id from chains)
