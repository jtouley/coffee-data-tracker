-- Warn only (eval/dq/README.md, #7): about 9 in 10 of the coffees it lists have a wrong price per 100 g, but the
-- rest are real (a Gesha, a Kona), and wrong ones are in every week's catalog, so blocking would hold back every
-- release.
{{ config(severity='warn', store_failures=true) }}
-- price_per_100g_in_range only re-checks dim_coffee's $0.50-$100 filter, so a misread weight or a foreign currency
-- inside that range passes every test. This compares each listed, non-wholesale coffee's price per 100 g with
-- coffees like it: the median and MAD of ln(price_per_100g) in its (origin_country, process) group when that has at
-- least price_outlier_min_group coffees, else in its origin_country group (any process) when that has, else over
-- all of them. Robust z = (ln price - median) / (1.4826 x MAD). Listed when z <= -price_outlier_low_z (mostly a bag
-- size read too large: "1/2 lb" as 907 g, a 5 lb price on a 12 oz bag, or not coffee) or z >= price_outlier_high_z
-- (mostly several bags priced as one, pesos or rand read as dollars, or merchandise). Real rare lots sit between 4
-- and 6 more often than above, hence the asymmetric bounds.
with p as (
    select coffee_id, roaster_id, origin_country, process, price_per_100g, ln(price_per_100g) as lp
    from {{ ref('int_current_catalog') }}
    where not is_wholesale and price_per_100g is not null
),
-- Each level's centre and spread over all of its coffees.
op as (
    select origin_country, process, count(*) as n, median(lp) as m from p
    where origin_country is not null and process is not null group by all
),
op_mad as (
    select g.*, median(abs(p.lp - g.m)) as mad from op g join p using (origin_country, process) group by all
),
o as (select origin_country, count(*) as n, median(lp) as m from p where origin_country is not null group by all),
o_mad as (select g.*, median(abs(p.lp - g.m)) as mad from o g join p using (origin_country) group by all),
a as (select median(lp) as m from p),
a_mad as (select a.m, median(abs(p.lp - a.m)) as mad from p, a group by a.m),
scored as (
    select p.coffee_id, p.roaster_id, p.origin_country, p.process, p.price_per_100g,
           case when g2.n >= {{ var('price_outlier_min_group') }} then 'origin_process'
                when g1.n >= {{ var('price_outlier_min_group') }} then 'origin' else 'all' end as compared_with,
           case when g2.n >= {{ var('price_outlier_min_group') }} then (p.lp - g2.m) / nullif(1.4826 * g2.mad, 0)
                when g1.n >= {{ var('price_outlier_min_group') }} then (p.lp - g1.m) / nullif(1.4826 * g1.mad, 0)
                else (p.lp - g0.m) / nullif(1.4826 * g0.mad, 0) end as z
    from p
    left join op_mad g2 using (origin_country, process)
    left join o_mad g1 using (origin_country)
    cross join a_mad g0
)
select coffee_id, roaster_id, origin_country, process, price_per_100g, compared_with, round(z, 2) as z
from scored
where z <= -{{ var('price_outlier_low_z') }} or z >= {{ var('price_outlier_high_z') }}
