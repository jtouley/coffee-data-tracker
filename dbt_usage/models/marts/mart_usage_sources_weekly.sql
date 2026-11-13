-- One row per (week, source_kind, source, utm_medium, utm_campaign): where visits came from, counted on each load's
-- first event (the one carrying the source). source_kind is utm, referrer, direct or internal (the collector's). A source is shown once it occurred on usage_min_days days in the window, and its medium and campaign
-- only when the whole combination did; otherwise '(rare)' (design section 3). Bots left out.
with landed as (
    select week, received_date, source_kind, source,
        coalesce(utm_medium, 'none') as utm_medium, coalesce(utm_campaign, 'none') as utm_campaign
    from {{ ref('int_usage_events') }}
    where source_kind is not null and not bot
),

source_days as {{ days_in_window('landed', ['source_kind', 'source']) }},

combo_days as {{ days_in_window('landed', ['source_kind', 'source', 'utm_medium', 'utm_campaign']) }},

judged as (
    select l.*,
        s.days >= {{ var('usage_min_days') }} as source_passes,
        c.days >= {{ var('usage_min_days') }} as combo_passes
    from landed as l
    inner join source_days as s
        on s.week = l.week and s.source_kind = l.source_kind and s.source is not distinct from l.source
    inner join combo_days as c
        on c.week = l.week and c.source_kind = l.source_kind and c.source is not distinct from l.source
        and c.utm_medium = l.utm_medium and c.utm_campaign = l.utm_campaign
)

select week, source_kind,
    if(source_passes, source, '(rare)') as source,
    if(source_passes and combo_passes, utm_medium, '(rare)') as utm_medium,
    if(source_passes and combo_passes, utm_campaign, '(rare)') as utm_campaign,
    count(*) as landings
from judged
group by all
