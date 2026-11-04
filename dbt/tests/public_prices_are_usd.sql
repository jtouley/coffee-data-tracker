-- No public price is in a currency other than USD, and no public roaster is outside the US. Checked against what
-- the store stated (stg_snapshots keeps each variant's own price and currency next to the USD-only price_usd):
--   * a public price event with a price, for a bag whose price that run wasn't in USD (is_usd), or with a previous
--     price, for a bag whose price in the run that price comes from (fct_price_event.previous_price_run) wasn't;
--   * a public coffee with a price, or a price per 100 g, whose latest listing has no bag priced in USD;
--   * a public roaster outside the US (stg_registry.country).
with obs as (
    select s.coffee_id, s.run_date, v.variant_id, v.is_usd, v.currency
    from {{ ref('stg_snapshots') }} s, unnest(s.variants) as t(v)
)
select 'public_price_event' as source, e.coffee_id, e.variant_id, e.run_date, o.currency
from {{ ref('public_price_event') }} e
join obs o using (coffee_id, variant_id, run_date)
where e.price_usd is not null and not o.is_usd
union all
select 'public_price_event (previous)', e.coffee_id, e.variant_id, e.run_date, o.currency
from {{ ref('public_price_event') }} e
join {{ ref('fct_price_event') }} f using (coffee_id, variant_id, run_date, event)
join obs o on o.coffee_id = f.coffee_id and o.variant_id = f.variant_id and o.run_date = f.previous_price_run
where e.previous_price_usd is not null and not o.is_usd
union all
select 'public_coffee', c.coffee_id, null, c.last_seen, null
from {{ ref('public_coffee') }} c
join {{ ref('stg_snapshots') }} s on s.coffee_id = c.coffee_id and s.run_date = c.last_seen
where (c.min_price_usd is not null or c.price_per_100g is not null)
  and not coalesce(list_bool_or(list_transform(s.variants, lambda v: v.is_usd)), false)
union all
select 'public_roaster', r.roaster_id, null, null, g.country
from {{ ref('public_roaster') }} r
join {{ ref('stg_registry') }} g using (roaster_id)
where g.country <> 'US'
