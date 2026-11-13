-- Volume band (warn): a roaster's week of click-outs above roaster_band_ratio x the median of its previous
-- band_weeks weeks, and above roaster_band_floor, may be a roaster inflating its own clicks (design section 4);
-- numbers are directional. Warn only: it becomes an error once 12 weeks of real clicks show how far an honest
-- roaster's week moves.
{{ config(severity='warn') }}
with weekly as (
    select week, roaster_id, sum(click_outs) as click_outs
    from {{ ref('mart_roaster_clicks_weekly') }}
    group by all
),

banded as (
    select w.week, w.roaster_id, w.click_outs, median(p.click_outs) as median_before
    from weekly as w
    left join weekly as p
        on p.roaster_id = w.roaster_id and p.week < w.week and p.week >= w.week - 7 * {{ var('band_weeks') }}
    group by all
)

select week, roaster_id, click_outs, median_before
from banded
where click_outs > {{ var('roaster_band_floor') }}
  and click_outs > {{ var('roaster_band_ratio') }} * coalesce(median_before, 0)
