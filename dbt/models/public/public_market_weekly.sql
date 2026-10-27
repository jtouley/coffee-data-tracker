-- Market summary by origin x process for coffees listed now (public coffees only).
with c as (select * from {{ ref('public_coffee') }} where is_listed and not is_blend and origin_country is not null)
select
    (select max(last_seen) from {{ ref('public_coffee') }}) as as_of_run,
    origin_country, coalesce(process, 'unknown') as process,
    count(*) as coffees, count(distinct roaster_id) as roasters, count(*) filter (where in_stock) as in_stock,
    round(median(price_per_100g), 2) as median_price_per_100g,
    round(quantile_cont(price_per_100g, 0.1), 2) as p10_price_per_100g,
    round(quantile_cont(price_per_100g, 0.9), 2) as p90_price_per_100g
from c
group by all
order by coffees desc
