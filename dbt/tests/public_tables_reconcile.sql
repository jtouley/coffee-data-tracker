-- Warn only for now (eval/dq/README.md, #4): no recorded build has a disagreement (runs 2026-10-26 to 11-02), so
-- there is nothing yet to measure its false alarms on; the owner's rule is that a rule warns until that is shown.
{{ config(severity='warn', store_failures=true) }}
-- Totals that two tables state separately must agree, so the site, the gate and a consumer joining the public
-- tables count the same coffees. One row per disagreement:
--   roaster_counts: public_roaster.coffees_listed and coffees_in_stock against public_coffee's own rows for the
--     roaster (listed and not archived, and in stock; wholesale left out on both sides);
--   listed_total: the sum of dim_roaster.coffees_listed against mart_build_profile's listed_coffees (the gate's
--     count), two definitions of "listed" that must not drift apart;
--   event_roaster: a public price event whose roaster_id isn't its public_coffee row's;
--   orphan_event: a price event whose coffee isn't in dim_coffee, unless the product's latest record is classified
--     as not coffee. That one is by design: dim_coffee drops a product reclassified as not coffee and keeps its past
--     events (fct_price_event's relationships test warns on every orphan, these included).
with listed as (
    select roaster_id,
           count(*) filter (where is_listed and catalog_status <> 'archived') as coffees_listed,
           count(*) filter (where in_stock) as coffees_in_stock
    from {{ ref('public_coffee') }} where not is_wholesale group by roaster_id
),
latest as (
    select s.coffee_id, {{ decided('s.decision', 'p.is_coffee') }} as is_coffee
    from {{ ref('stg_snapshots') }} s join {{ ref('stg_products') }} p using (input_hash)
    qualify row_number() over (partition by s.coffee_id order by s.run_date desc) = 1
),
orphans as (
    select e.coffee_id, any_value(e.roaster_id) as roaster_id, count(*) as events
    from {{ ref('fct_price_event') }} e
    where not exists (select 1 from {{ ref('dim_coffee') }} d where d.coffee_id = e.coffee_id)
    group by e.coffee_id
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
union all
select 'orphan_event', o.coffee_id, format('{} events; the product is still coffee or has no record', o.events)
from orphans o left join latest l using (coffee_id)
where l.is_coffee is distinct from false
