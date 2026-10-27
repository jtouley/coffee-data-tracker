-- One row per bag, per user: how many shots it took, the best one (highest rating, latest wins ties, as in
-- mart_dial_in), and how many days off roast the first and last shots were. Personal: private marts only.
with b as (select * from {{ ref('stg_bags') }}),
s as (select * from {{ ref('fct_shot') }} where bag_id is not null),
per_bag as (
    select
        user_id, bag_id, count(*) as shot_count, min(brewed_at) as first_shot_at, max(brewed_at) as last_shot_at,
        arg_max(shot_id, (rating, brewed_at)) as best_shot_id, max(rating) as best_rating,
        round(avg(rating), 2) as avg_rating, round(avg(time_s), 1) as avg_time_s,
        count(*) filter (where extraction_time_bucket = 'target') as shots_in_window
    from s group by user_id, bag_id
)
select
    b.user_id, b.bag_id, b.coffee_id, b.coffee_freetext, c.title as coffee_title, c.roaster_name, b.roast_date,
    b.opened_on, b.finished_on, b.photo_id is not null as has_photo,
    coalesce(p.shot_count, 0) as shot_count, p.shots_in_window, p.best_shot_id, p.best_rating, p.avg_rating,
    p.avg_time_s, p.first_shot_at, p.last_shot_at,
    date_diff('day', b.roast_date, cast(p.first_shot_at as date)) as days_off_roast_first,
    date_diff('day', b.roast_date, cast(p.last_shot_at as date)) as days_off_roast_last
from b
left join per_bag p on p.bag_id = b.bag_id and p.user_id = b.user_id
left join {{ ref('dim_coffee') }} c using (coffee_id)
