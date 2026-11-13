-- One row per (week, roaster_id, surface, device, source, country, region): click-outs to a roaster's store, by
-- where they were clicked, the device, the traffic source (when the click was its load's first event; else
-- 'not recorded') and the visitor's country and region. Never a city (design section 5). Bots left out.
with clicks as (
    select e.*, p.country as place_country, p.region as place_region
    from {{ ref('int_usage_events') }} as e
    inner join {{ ref('int_usage_event_places') }} as p on p.event_id = e.event_id
    where e.event = 'click_out' and e.roaster_id is not null and not e.bot
),

landed as (
    select week, received_date, source_kind, source
    from {{ ref('int_usage_events') }}
    where source_kind is not null and not bot
),

source_days as {{ days_in_window('landed', ['source_kind', 'source']) }}

select c.week, c.roaster_id, coalesce(c.surface, 'unknown') as surface, coalesce(c.device, 'unknown') as device,
    case
        when c.source_kind is null then 'not recorded'
        when s.days >= {{ var('usage_min_days') }} then c.source
        else '(rare)'
    end as source,
    c.place_country as country, c.place_region as region,
    count(*) as click_outs
from clicks as c
left join source_days as s
    on s.week = c.week and s.source_kind = c.source_kind and s.source is not distinct from c.source
group by all
