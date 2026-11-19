-- The seed list (config/roasters.csv), one row per roaster it names: the hand-kept and Overture-derived roasters
-- stg_registry starts from. Empty text reads as null.
select roaster_id, name, nullif(city, '') as city, nullif(state, '') as state,
       coalesce(try_cast(locations as integer), 1) as locations, coalesce(opted_out, 'false') = 'true' as opted_out,
       nullif(final_host, '') as seed_host, nullif(platform, '') as seed_platform,
       try_cast(nullif(last_probed, '') as date) as seed_probed, sources, nullif(country, '') as country,
       nullif(lower(trim(alias_of)), '') as alias_of
from read_csv('{{ var("config_dir") }}/roasters.csv', header = true, all_varchar = true)
