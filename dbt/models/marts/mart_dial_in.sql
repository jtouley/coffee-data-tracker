-- Your best recipe per coffee, and per origin x process across coffees.
with s as (select *, coalesce(coffee_id, 'freetext:' || lower(coffee_freetext)) as coffee_key from {{ ref('fct_shot') }}),
per_coffee as (
    select
        'coffee' as grain, coffee_key as key, any_value(coalesce(coffee_title, coffee_freetext)) as label,
        count(*) as shots, round(avg(rating), 2) as avg_rating, max(rating) as best_rating,
        arg_max(grinder_setting, rating) as best_grind, arg_max(dose_g, rating) as best_dose_g,
        arg_max(yield_g, rating) as best_yield_g, arg_max(time_s, rating) as best_time_s,
        round(avg(brew_ratio), 2) as avg_ratio, max(brewed_at) as last_brewed_at
    from s group by coffee_key
),
per_style as (
    select
        'origin_process' as grain, coalesce(origin_country, '?') || ' / ' || coalesce(process, '?') as key,
        coalesce(origin_country, '?') || ' / ' || coalesce(process, '?') as label,
        count(*), round(avg(rating), 2), max(rating), arg_max(grinder_setting, rating), arg_max(dose_g, rating),
        arg_max(yield_g, rating), arg_max(time_s, rating), round(avg(brew_ratio), 2), max(brewed_at)
    from s where coffee_id is not null group by all
)
select * from per_coffee
union all
select * from per_style
