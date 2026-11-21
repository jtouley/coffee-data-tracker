-- One row per screen with its features (docs/design/roaster-screen.md, section 4.1): how much of the catalog was
-- seen, how much of it is coffee named as coffee, in what bags, under whose name, and what the home page says. The
-- same arithmetic as coffeedata.screen_rules.features, which the evaluation uses; a dbt unit test and
-- tests/test_screen_rules.py hold the two to the same numbers. Shares are over the named coffees (0 when there are
-- none). is_latest marks each place's latest screen: the newest screened_at, then batch_id, then screen_id, so two
-- screens in the same second always give the same answer. Nothing here reads the clock.
with products as (
    select screen_id,
        count(*) as products,
        count(*) filter (where is_coffee) as coffees,
        count(*) filter (where named) as named,
        count(*) filter (where named and len(bag_grams) > 0) as bagged,
        count(*) filter (where named and roast_level is not null) as roasted,
        count(*) filter (where named and own_vendor) as own_vendor,
        count(*) filter (where named and other_roaster) as other_roaster,
        count(*) filter (where named and is_wholesale) as wholesale,
        count(distinct vendor_key) filter (where named and vendor_key <> '') as vendors,
        count(distinct origin_country) filter (where named) as origins,
        list_sort(list_distinct(flatten(list(bag_grams) filter (where named)))) as bag_sizes,
        list_sort(list_distinct(flatten(list(currencies)))) as currencies
    from {{ ref('int_screen_products') }}
    group by screen_id
),
origin_counts as (
    select screen_id, origin_country, count(*) as n
    from {{ ref('int_screen_products') }}
    where named and origin_country is not null
    group by screen_id, origin_country
),
origin_repeats as (
    select screen_id, count(*) as origin_repeat from origin_counts where n >= 2 group by screen_id
)
select s.screen_id, s.roaster_id, s.screened_at, s.batch_id, s.status, s.refusal, s.platform, s.page_full,
    s.page_timeout,
    coalesce(p.products, 0) as products,
    coalesce(p.coffees, 0) as coffees,
    coalesce(p.named, 0) as named,
    case when coalesce(p.products, 0) > 0 then p.named / p.products else 0.0 end as coffee_share,
    case when coalesce(p.named, 0) > 0 then p.bagged / p.named else 0.0 end as bag_share,
    coalesce(p.bag_sizes, []::integer[]) as bag_sizes,
    case when coalesce(p.named, 0) > 0 then p.roasted / p.named else 0.0 end as roast_share,
    coalesce(p.origins, 0) as origins,
    coalesce(r.origin_repeat, 0) as origin_repeat,
    coalesce(p.own_vendor, 0) as own_vendor,
    case when coalesce(p.named, 0) > 0 then p.own_vendor / p.named else 0.0 end as own_vendor_share,
    coalesce(p.other_roaster, 0) as other_roaster,
    coalesce(p.vendors, 0) as vendors,
    case when coalesce(p.named, 0) > 0 then p.wholesale / p.named else 0.0 end as wholesale_share,
    coalesce(p.currencies, []::varchar[]) as currencies,
    h.roast_phrase,
    h.serves_phrase,
    pl.tags as place_tags,
    pl.locations,
    row_number() over (
        partition by s.roaster_id order by s.screened_at desc, s.batch_id desc, s.screen_id desc
    ) = 1 as is_latest
from {{ ref('stg_screens') }} s
left join products p using (screen_id)
left join origin_repeats r using (screen_id)
left join {{ ref('int_screen_home') }} h using (screen_id)
left join {{ ref('stg_screen_places') }} pl on pl.roaster_id = s.roaster_id
