-- The specialty market by origin x process, for the current catalog (listed, not the roasters' archived back
-- catalog): breadth, price and flavor language.
with c as (
    select * from {{ ref('int_current_catalog') }}
    where not is_wholesale and not is_blend and origin_country is not null and not is_chain
),
notes as (
    select origin_country, note, count(*) as n
    from (select origin_country, unnest(tasting_notes) as note from c)
    group by origin_country, note
    qualify row_number() over (partition by origin_country order by n desc, note) <= 5
)
select
    c.origin_country, coalesce(c.process, 'unknown') as process,
    count(*) as coffees, count(distinct c.roaster_id) as roasters,
    count(*) filter (where c.in_stock) as in_stock,
    round(median(c.price_per_100g), 2) as median_price_per_100g,
    round(quantile_cont(c.price_per_100g, 0.1), 2) as p10_price_per_100g,
    round(quantile_cont(c.price_per_100g, 0.9), 2) as p90_price_per_100g,
    (select list(note order by n desc, note) from notes n where n.origin_country = c.origin_country) as top_notes_for_origin,
    {{ release_columns() }}
from c
group by all
order by coffees desc
