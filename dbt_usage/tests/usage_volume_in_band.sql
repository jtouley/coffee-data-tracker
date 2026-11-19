-- Volume band (warn): a week's events (bots left out) outside 1/volume_band_ratio to volume_band_ratio x the median
-- of the previous band_weeks weeks, once band_min_weeks of them exist: a beacon that stopped sending, or a flood the
-- bot flag missed. Warn only, like the catalog's bands, until 12 weeks of real traffic set the ratio.
{{ config(severity='warn') }}
with weekly as (
    select week, count(*) as events from {{ ref('int_usage_events') }} where not bot group by week
),

banded as (
    select w.week, w.events, median(p.events) as median_before, count(p.week) as weeks_before
    from weekly as w
    left join weekly as p on p.week < w.week and p.week >= w.week - 7 * {{ var('band_weeks') }}
    group by all
)

select week, events, median_before
from banded
where weeks_before >= {{ var('band_min_weeks') }}
  and (events > {{ var('volume_band_ratio') }} * median_before
       or events < median_before / {{ var('volume_band_ratio') }})
