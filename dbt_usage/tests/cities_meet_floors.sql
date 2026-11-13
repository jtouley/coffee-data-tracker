-- Every city a mart names met the city floors (design section 5): city_min_population people in the pinned GeoNames
-- file, events on city_min_days distinct days in the window and city_min_week_events in the week; every city x facet
-- cell holds city_cell_min_events events; and no hidden cell can be worked out by subtraction: for each place, week
-- and facet shown, the events not shown (its total, recomputed from int_usage_events, less the cells shown) are 0 or
-- at least city_cell_min_events, and so is a place's events less its page views.
-- Across places, against the totals other marts publish: no week has exactly one place's page views held back (the
-- week's total less the places shown would be that place's), no week and facet has exactly one place's facet held
-- back, and '(other)' holds the same values at every place (a value named at one place is named wherever it occurs),
-- so a facet value's published total less the cells shown is never one place's cell.
-- Recomputed from int_usage_events, int_usage_event_places and the geo tables, not the marts' SQL. Returns one row
-- per breach.
{{ config(severity='error') }}
{%- set w = var('usage_window_days') %}
with ev as (
    select * from {{ ref('int_usage_events') }} where not bot
),

landed as (
    select week, received_date, source_kind, source from ev where source_kind is not null
),

source_days as {{ days_in_window('landed', ['source_kind', 'source']) }},

-- every event's cell in each facet it belongs to, labelled as the facet mart labels it
truth_cells as (
    select e.week, p.country, p.region, p.city, f.facet,
        case f.facet
            when 'surface' then coalesce(e.surface, 'unknown')
            when 'device' then coalesce(e.device, 'unknown')
            when 'origin' then coalesce(e.origin, 'any')
            when 'process' then coalesce(e.process, 'any')
            when 'in_stock' then if(e.in_stock, 'yes', 'any')
            when 'sort' then coalesce(e.sort, 'default')
            else if(s.days >= {{ var('usage_min_days') }}, e.source, '(rare)')
        end as facet_value,
        count(*) as events
    from ev as e
    inner join {{ ref('int_usage_event_places') }} as p on p.event_id = e.event_id
    inner join (values ('surface'), ('device'), ('origin'), ('process'), ('in_stock'), ('sort'), ('source'))
        as f(facet)
        on f.facet in ('surface', 'device')
        or (f.facet = 'source' and e.source_kind is not null)
        or (f.facet not in ('surface', 'device', 'source') and e.event in ('search', 'filter_state'))
    left join source_days as s
        on f.facet = 'source' and s.week = e.week and s.source_kind = e.source_kind
        and s.source is not distinct from e.source
    group by all
),

facet_places as (
    select t.week, t.country, t.region, t.city, t.facet, bool_or(m.week is not null) as shown
    from (select distinct week, country, region, city, facet from truth_cells) as t
    left join (select distinct week, country, region, city, facet from {{ ref('mart_usage_city_facets_weekly') }}) as m
        on m.week = t.week and m.country = t.country and m.region = t.region and m.city = t.city
        and m.facet = t.facet
    group by all
),

named as (
    select distinct week, country, region, city from {{ ref('mart_usage_locations_weekly') }}
    where city not like 'other%' and city <> 'unknown'
    union
    select distinct week, country, region, city from {{ ref('mart_usage_city_facets_weekly') }}
    where city not like 'other%' and city <> 'unknown'
),

measured as (
    select n.week, n.country, n.region, n.city,
        max(c.population) as population,
        count(distinct e.received_date) filter (
            where e.received_date between n.week + 6 - ({{ w }} - 1) and n.week + 6) as days,
        count(*) filter (where e.week = n.week) as week_events
    from named as n
    left join {{ ref('stg_geo_cities') }} as c
        on c.country = n.country and c.region = n.region and c.city = lower(n.city)
    left join ev as e on e.country = n.country and e.region = n.region and e.city = n.city
    group by all
)

select 'city' as rule, week, coalesce(population, 0) as population, days, week_events as events
from measured
where coalesce(population, 0) < {{ var('city_min_population') }}
   or days < {{ var('city_min_days') }}
   or week_events < {{ var('city_min_week_events') }}
union all
select 'cell', week, null, null, events
from {{ ref('mart_usage_city_facets_weekly') }}
where events < {{ var('city_cell_min_events') }}
union all
select 'hidden by subtraction', s.week, null, null, t.events - s.shown
from (
    select week, country, region, city, facet, sum(events) as shown
    from {{ ref('mart_usage_city_facets_weekly') }}
    group by all
) as s
inner join (
    -- each facet's total: every event for surface, device and source (on events that carry a source), the result
    -- events (search, filter_state) for the filters
    select e.week, p.country, p.region, p.city, f.facet, count(*) as events
    from ev as e
    inner join {{ ref('int_usage_event_places') }} as p on p.event_id = e.event_id
    inner join (values ('surface'), ('device'), ('origin'), ('process'), ('in_stock'), ('sort'), ('source'))
        as f(facet)
        on f.facet in ('surface', 'device')
        or (f.facet = 'source' and e.source_kind is not null)
        or (f.facet not in ('surface', 'device', 'source') and e.event in ('search', 'filter_state'))
    group by all
) as t
    on t.week = s.week and t.country = s.country and t.region = s.region and t.city = s.city and t.facet = s.facet
where t.events - s.shown between 1 and {{ var('city_cell_min_events') }} - 1
union all
select 'page views', week, null, null, events
from {{ ref('mart_usage_locations_weekly') }}
where page_views < {{ var('city_cell_min_events') }} or events - page_views < {{ var('city_cell_min_events') }}
union all
select 'page views held back alone', l.week, null, null, t.page_views - l.shown
from (
    select week, count(*) as places, count(*) filter (where page_views is null) as held,
        coalesce(sum(page_views), 0) as shown
    from {{ ref('mart_usage_locations_weekly') }}
    group by all
) as l
inner join (
    select week, sum(page_views) as page_views from {{ ref('mart_usage_page_views_weekly') }} group by all
) as t on t.week = l.week
where l.held = 1 and l.places > 1
union all
select 'facet held back alone', week, null, null, null
from facet_places
group by week, facet
having count(*) filter (where not shown) = 1 and count(*) > 1
union all
select '(other) differs by place', c.week, null, null, c.events
from truth_cells as c
inner join facet_places as p
    on p.week = c.week and p.country = c.country and p.region = c.region and p.city = c.city and p.facet = c.facet
    and p.shown
inner join (
    select distinct week, facet, facet_value from {{ ref('mart_usage_city_facets_weekly') }}
    where facet_value <> '(other)'
) as n on n.week = c.week and n.facet = c.facet and n.facet_value = c.facet_value
anti join {{ ref('mart_usage_city_facets_weekly') }} as m
    on m.week = c.week and m.country = c.country and m.region = c.region and m.city = c.city
    and m.facet = c.facet and m.facet_value = c.facet_value
