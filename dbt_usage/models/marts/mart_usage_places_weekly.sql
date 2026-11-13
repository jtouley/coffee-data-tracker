-- One row per (week, place_type, place): the catalog cities and states the page's parser read from search text
-- (place_type 'city' or 'state'), lowercased. A place is shown only once it was searched on usage_min_days distinct
-- days in the window; the rest count as '(rare)'. These are places people search for, not where they are.
with named as (
    select week, received_date, 'city' as place_type, lower(trim(unnest(cities))) as place
    from {{ ref('int_usage_events') }}
    where event = 'search' and not bot
    union all
    select week, received_date, 'state' as place_type, lower(trim(unnest(states))) as place
    from {{ ref('int_usage_events') }}
    where event = 'search' and not bot
),

places as (
    select * from named where place is not null and place <> ''
),

days as {{ days_in_window('places', ['place_type', 'place']) }}

select p.week, p.place_type,
    if(d.days >= {{ var('usage_min_days') }}, p.place, '(rare)') as place,
    count(*) as searches
from places as p
inner join days as d on d.week = p.week and d.place_type = p.place_type and d.place = p.place
group by all
