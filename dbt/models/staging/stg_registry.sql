-- Seed list (config/roasters.csv) plus roasters added in the lake by discovery or by hand.
with seed as (
    select roaster_id, name, nullif(city, '') as city, nullif(state, '') as state,
           coalesce(try_cast(locations as integer), 1) as locations, coalesce(opted_out, 'false') = 'true' as opted_out,
           nullif(final_host, '') as seed_host, nullif(platform, '') as seed_platform,
           try_cast(nullif(last_probed, '') as date) as seed_probed, sources, nullif(country, '') as country,
           nullif(lower(trim(alias_of)), '') as alias_of
    from read_csv('{{ var("config_dir") }}/roasters.csv', header = true, all_varchar = true)
),
-- Every pick has a total order (the newest row; on a tie in added_at, the value itself), and sources are listed
-- alphabetically: the same lake builds the same table whatever order DuckDB reads its rows in.
added as (
    select roaster_id, arg_max(name, (added_at, name)) as name, arg_max(city, (added_at, city)) as city,
           arg_max(state, (added_at, state)) as state, max(locations) as locations, bool_or(opted_out) as opted_out,
           string_agg(distinct source, '+' order by source) as sources, min(added_at) as first_added,
           min(added_at) filter (where opted_out) as opted_out_at
    from {{ lake('registry') }}
    where roaster_id is not null
    group by roaster_id
)
select
    coalesce(s.roaster_id, a.roaster_id) as roaster_id, coalesce(a.name, s.name) as roaster_name,
    coalesce(a.city, s.city) as city, coalesce(a.state, s.state) as state,
    -- The store count: the US Overture places that list this website and are roasteries (tagged coffee_roastery, or
    -- a coffee place with "roast" in its name: discover.overture), as the seed recorded it; for a roaster the lake
    -- alone lists, the most any discovery row counted (an OSM row counts 1). A cafe not tagged as a roastery isn't
    -- counted, so a chain of cafes can read low (seeds/chain_overrides.csv). int_roaster_state decides is_chain.
    coalesce(s.locations, a.locations, 1) as locations,
    coalesce(a.opted_out, false) or coalesce(s.opted_out, false) as opted_out,
    concat_ws('+', s.sources, a.sources) as sources, s.seed_host, s.seed_platform, s.seed_probed,
    -- ISO 3166-1 alpha-2. The seed states it per roaster (a hand-checked correction for a store outside the US);
    -- a roaster the lake alone lists came from discovery, which keeps US places only.
    coalesce(s.country, 'US') as country,
    -- Hand-checked: another roaster_id that serves the same store (a rebrand whose old domain doesn't redirect, so
    -- the resolved host can't show it). dim_roaster makes this roaster an alias of it; null: decided by host alone.
    s.alias_of,
    a.first_added,  -- when the lake first listed it (null: from the seed list alone)
    -- when the lake first recorded its opt-out; null: not opted out, or opted out in the seed list, which has no date
    case when not coalesce(s.opted_out, false) then a.opted_out_at end as opted_out_at
from seed s
full outer join added a using (roaster_id)
