-- Weekly price / stock / listing changes for public coffees.
select e.run_date, e.coffee_id, e.roaster_id, e.variant_id, e.event, e.price_usd,
       e.previous_price_usd, e.available
from {{ ref('fct_price_event') }} e
where e.coffee_id in (select coffee_id from {{ ref('public_coffee') }})  -- public roasters, coffees with a safe link
