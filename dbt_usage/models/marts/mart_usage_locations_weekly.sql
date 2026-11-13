-- One row per (week, country, region, city): where events came from, by the places int_usage_event_places allows
-- (a city under 50,000 people or below the floors counts as "other, <region>"). City is where the IP block is
-- routed, often the provider's nearest metro: read "Denver" as "Denver-area networks". Bots left out.
select e.week, p.country, p.region, p.city,
    count(*) as events,
    count(*) filter (where e.event = 'page_view') as page_views
from {{ ref('int_usage_events') }} as e
inner join {{ ref('int_usage_event_places') }} as p on p.event_id = e.event_id
where not e.bot
group by all
