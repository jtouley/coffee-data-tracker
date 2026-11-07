-- Seed list (config/roasters.csv) plus roasters added in the lake by discovery or by hand.
with seed as (
    select roaster_id, name, nullif(city, '') as city, nullif(state, '') as state,
           coalesce(try_cast(locations as integer), 1) as locations,
           coalesce(is_chain, 'false') = 'true' as is_chain, coalesce(opted_out, 'false') = 'true' as opted_out,
           nullif(final_host, '') as seed_host, nullif(platform, '') as seed_platform,
           try_cast(nullif(last_probed, '') as date) as seed_probed, sources, nullif(country, '') as country
    from read_csv('{{ var("config_dir") }}/roasters.csv', header = true, all_varchar = true)
),
-- Every pick has a total order (the newest row; on a tie in added_at, the value itself), and sources are listed
-- alphabetically: the same lake builds the same table whatever order DuckDB reads its rows in.
added as (
    select roaster_id, arg_max(name, (added_at, name)) as name, arg_max(city, (added_at, city)) as city,
           arg_max(state, (added_at, state)) as state, max(locations) as locations, bool_or(opted_out) as opted_out,
           string_agg(distinct source, '+' order by source) as sources, min(added_at) as first_added
    from {{ lake('registry') }}
    where roaster_id is not null
    group by roaster_id
)
select
    coalesce(s.roaster_id, a.roaster_id) as roaster_id, coalesce(a.name, s.name) as roaster_name,
    coalesce(a.city, s.city) as city, coalesce(a.state, s.state) as state,
    coalesce(s.locations, a.locations, 1) as locations, coalesce(s.is_chain, false) as is_chain,
    coalesce(a.opted_out, false) or coalesce(s.opted_out, false) as opted_out,
    concat_ws('+', s.sources, a.sources) as sources, s.seed_host, s.seed_platform, s.seed_probed,
    -- ISO 3166-1 alpha-2. The seed states it per roaster (a hand-checked correction for a store outside the US);
    -- a roaster the lake alone lists came from discovery, which keeps US places only.
    coalesce(s.country, 'US') as country,
    a.first_added  -- when the lake first listed it (null: from the seed list alone)
from seed s
full outer join added a using (roaster_id)
