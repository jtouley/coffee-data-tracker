select
    r.*,
    count(c.coffee_id) as coffees_listed,
    count(c.coffee_id) filter (where c.in_stock) as coffees_in_stock
from {{ ref('stg_roasters') }} r
left join {{ ref('dim_coffee') }} c using (roaster_slug)
group by all
