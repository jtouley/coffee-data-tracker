-- One row per (kind, id) of the served release the clicks are checked against: kind 'roaster' (dim_roaster) or
-- 'coffee' (dim_coffee), with the text its vocabulary words come from. Empty when the build is given no release.
with roasters as (
    select 'roaster' as kind, cast(roaster_id as varchar) as id,
        concat_ws(' ', roaster_name, city, state) as words_text
    from {{ catalog_file('catalog_roasters', ['roaster_id', 'roaster_name', 'city', 'state']) }}
),

coffees as (
    select 'coffee' as kind, cast(coffee_id as varchar) as id,
        concat_ws(' ', title, origin_country, process) as words_text
    from {{ catalog_file('catalog_coffees', ['coffee_id', 'title', 'origin_country', 'process']) }}
)

select * from roasters
union all
select * from coffees
