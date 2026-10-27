-- Your best recipe per coffee and grinder, and per origin x process across coffees, for each user. A null
-- grinder_id is the grinder used before equipment was recorded.
with s as (select *, coalesce(coffee_id, 'freetext:' || lower(coffee_freetext)) as coffee_key from {{ ref('fct_shot') }}),
per_coffee as (
    select
        user_id, grinder_id, any_value(grinder_name) as grinder_name,
        'coffee' as grain, coffee_key as key, any_value(coalesce(coffee_title, coffee_freetext)) as label,
        count(*) as shots, round(avg(rating), 2) as avg_rating, max(rating) as best_rating,
        arg_max(grinder_setting, rating) as best_grind, arg_max(dose_g, rating) as best_dose_g,
        arg_max(yield_g, rating) as best_yield_g, arg_max(time_s, rating) as best_time_s,
        round(avg(brew_ratio), 2) as avg_ratio, max(brewed_at) as last_brewed_at
    from s group by user_id, grinder_id, coffee_key
),
per_style as (
    select
        user_id, grinder_id, any_value(grinder_name),
        'origin_process', coalesce(origin_country, '?') || ' / ' || coalesce(process, '?'),
        coalesce(origin_country, '?') || ' / ' || coalesce(process, '?'),
        count(*), round(avg(rating), 2), max(rating), arg_max(grinder_setting, rating), arg_max(dose_g, rating),
        arg_max(yield_g, rating), arg_max(time_s, rating), round(avg(brew_ratio), 2), max(brewed_at)
    from s where coffee_id is not null
    group by user_id, grinder_id, coalesce(origin_country, '?') || ' / ' || coalesce(process, '?')
)
select * from per_coffee
union all
select * from per_style
