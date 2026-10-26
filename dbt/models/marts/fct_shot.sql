select
    s.*,
    round(s.yield_g / s.dose_g, 2) as brew_ratio,
    date_diff('day', s.roast_date, cast(s.brewed_at as date)) as days_off_roast,
    case when s.time_s < 25 then 'fast' when s.time_s <= 32 then 'target' else 'slow' end as extraction_time_bucket,
    c.roaster_slug, c.roaster_name, c.title as coffee_title, c.origin_country, c.process, c.roast_level
from {{ ref('stg_shots') }} s
left join {{ ref('dim_coffee') }} c using (coffee_id)
