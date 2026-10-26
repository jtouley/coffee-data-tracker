-- Seed list (config/roasters.csv) plus roasters added in the lake by discovery or by hand.
with seed as (
    select roaster_id, name, nullif(city, '') as city, nullif(state, '') as state,
           coalesce(try_cast(locations as integer), 1) as locations,
           coalesce(is_chain, 'false') = 'true' as is_chain, coalesce(opted_out, 'false') = 'true' as opted_out,
           nullif(final_host, '') as seed_host, nullif(platform, '') as seed_platform,
           try_cast(nullif(last_probed, '') as date) as seed_probed, sources
    from read_csv('{{ var("config_dir") }}/roasters.csv', header = true, all_varchar = true)
),
added as (
    select roaster_id, arg_max(name, added_at) as name, arg_max(city, added_at) as city,
           arg_max(state, added_at) as state, max(locations) as locations, bool_or(opted_out) as opted_out,
           string_agg(distinct source, '+') as sources
    from {{ lake('registry') }}
    where roaster_id is not null
    group by roaster_id
)
select
    coalesce(s.roaster_id, a.roaster_id) as roaster_id, coalesce(a.name, s.name) as roaster_name,
    coalesce(a.city, s.city) as city, coalesce(a.state, s.state) as state,
    coalesce(s.locations, a.locations, 1) as locations, coalesce(s.is_chain, false) as is_chain,
    coalesce(a.opted_out, false) or coalesce(s.opted_out, false) as opted_out,
    concat_ws('+', s.sources, a.sources) as sources, s.seed_host, s.seed_platform, s.seed_probed
from seed s
full outer join added a using (roaster_id)
