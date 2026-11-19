-- Warn only for now (eval/dq/README.md, #4): no recorded build has a disagreement (runs 2026-10-26 to 11-02), so
-- there is nothing yet to measure its false alarms on; the owner's rule is that a rule warns until that is shown.
{{ config(severity='warn', store_failures=true) }}
-- Totals that two tables state separately must agree, so the site, the gate and a consumer joining the public
-- tables count the same coffees. One row per disagreement:
--   roaster_counts: public_roaster.coffees_listed and coffees_in_stock against public_coffee's own rows for the
--     roaster (listed and not archived, and in stock; wholesale left out on both sides);
--   listed_total: the sum of dim_roaster.coffees_listed against mart_build_profile's listed_coffees (the gate's
--     count), two definitions of "listed" that must not drift apart;
--   event_roaster: a public price event whose roaster_id isn't its public_coffee row's.
-- A price event whose coffee isn't in dim_coffee is no longer checked here: fct_price_event keeps only coffees in
-- dim_coffee (a product reclassified as not coffee takes its events with it), and its relationships test to
-- dim_coffee is an error, so a build with such an event fails there.
with listed as (
    select roaster_id,
           count(*) filter (where is_listed and catalog_status <> 'archived') as coffees_listed,
           count(*) filter (where in_stock) as coffees_in_stock
    from {{ ref('public_coffee') }} where not is_wholesale group by roaster_id
)
select 'roaster_counts' as check_name, r.roaster_id as item,
       format('coffees_listed {} vs {} rows; coffees_in_stock {} vs {} rows', r.coffees_listed,
              coalesce(l.coffees_listed, 0), r.coffees_in_stock, coalesce(l.coffees_in_stock, 0)) as detail
from {{ ref('public_roaster') }} r left join listed l using (roaster_id)
where r.coffees_listed <> coalesce(l.coffees_listed, 0) or r.coffees_in_stock <> coalesce(l.coffees_in_stock, 0)
union all
select 'listed_total', 'dim_roaster', format('sum of coffees_listed {} vs listed_coffees {}', d.n, m.value)
from (select coalesce(sum(coffees_listed), 0) as n from {{ ref('dim_roaster') }}) d
cross join (select value from {{ ref('mart_build_profile') }} where metric = 'listed_coffees') m
where d.n <> m.value
union all
select 'event_roaster', e.coffee_id, format('event roaster {} vs coffee roaster {}', e.roaster_id, c.roaster_id)
from {{ ref('public_price_event') }} e join {{ ref('public_coffee') }} c using (coffee_id)
where e.roaster_id <> c.roaster_id
group by all
