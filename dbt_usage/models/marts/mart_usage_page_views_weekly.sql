-- One row per (week, surface, device): site page views, bots left out. The denominator for the other reports.
select week, coalesce(surface, 'unknown') as surface, coalesce(device, 'unknown') as device, count(*) as page_views
from {{ ref('int_usage_events') }}
where event = 'page_view' and not bot
group by all
