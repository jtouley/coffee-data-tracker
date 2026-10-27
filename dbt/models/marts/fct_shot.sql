-- One row per current shot, with its bag, grinder and catalog coffee. A grind setting only means something on
-- its own grinder; grind_position (0 to 1 along the dial) is a rough comparison across grinders.
with s as (
    -- The shot's own values are kept too (shot_*), so the app can start from this export without taking the bag's
    -- coffee and roast date into a shot that didn't have them.
    select s.coffee_id as shot_coffee_id, s.coffee_freetext as shot_coffee_freetext, s.roast_date as shot_roast_date,
    s.* replace (
        coalesce(s.coffee_id, b.coffee_id) as coffee_id,
        coalesce(s.coffee_freetext, b.coffee_freetext) as coffee_freetext,
        coalesce(s.roast_date, b.roast_date) as roast_date
    )
    from {{ ref('stg_shots') }} s
    left join {{ ref('stg_bags') }} b on b.bag_id = s.bag_id and b.user_id = s.user_id
)
select
    s.*,
    round(s.yield_g / s.dose_g, 2) as brew_ratio,
    date_diff('day', s.roast_date, cast(s.brewed_at as date)) as days_off_roast,
    case when s.time_s < 25 then 'fast' when s.time_s <= 32 then 'target' else 'slow' end as extraction_time_bucket,
    g.name as grinder_name,
    round((s.grinder_setting - g.scale_min) / nullif(g.scale_max - g.scale_min, 0), 3) as grind_position,
    c.roaster_id, c.roaster_name, c.title as coffee_title, c.origin_country, c.process, c.roast_level
from s
left join {{ ref('dim_equipment') }} g on g.equipment_id = s.grinder_id and g.user_id = s.user_id
left join {{ ref('dim_coffee') }} c using (coffee_id)
