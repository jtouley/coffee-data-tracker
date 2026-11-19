{% set fields = ['name', 'city', 'state'] -%}
-- Seed list (config/roasters.csv: stg_roaster_seed) plus roasters added in the lake by discovery or by hand
-- (stg_registry_records).
with seed as (select * from {{ ref('stg_roaster_seed') }}),
-- Every pick has a total order (the newest row; on a tie in added_at, the value itself), and sources are listed
-- alphabetically: the same lake builds the same table whatever order DuckDB reads its rows in.
-- name, city and state are picked twice: from the open records alone (is_open_source: Overture, curated, manual) and
-- from every record. arg_max skips nulls, so each is the newest non-null value per field, and two fields can come
-- from different records. The select below decides which pick a roaster gets.
added as (
    select roaster_id,
    {%- for f in fields %}
           arg_max({{ f }}, (added_at, {{ f }})) filter (where is_open_source) as open_{{ f }},
           arg_max({{ f }}, (added_at, {{ f }})) as {{ f }},
    {%- endfor %}
           max(locations) as locations, bool_or(opted_out) as opted_out,
           string_agg(distinct source, '+' order by source) as sources, min(added_at) as first_added,
           min(added_at) filter (where opted_out) as opted_out_at
    from {{ ref('stg_registry_records') }}
    group by roaster_id
)
{#- is_open: known from an open source, in the seed or the lake, so the roaster can reach the public tables
    (is_publishable). seed_open: the seed's own sources are open (all of config/roasters.csv today). #}
{%- set is_open = is_open_source("concat_ws('+', s.sources, a.sources)") %}
{%- set seed_open = is_open_source('s.sources') %}
select
    coalesce(s.roaster_id, a.roaster_id) as roaster_id,
    -- Field-level provenance (#90). A roaster known from an open source gets open values only: per field, the lake's
    -- newest non-null open value, else the seed's when its sources are open. A later OpenStreetMap record (ODbL)
    -- never replaces them, and a value only OpenStreetMap gave stays null. A roaster known only from OpenStreetMap
    -- never reaches the public tables; it keeps the lake's newest values, else the seed's.
    {%- for f in fields %}
    case when {{ is_open }} then coalesce(a.open_{{ f }}, case when {{ seed_open }} then s.{{ f }} end)
         else coalesce(a.{{ f }}, s.{{ f }}) end as {{ 'roaster_name' if f == 'name' else f }},
    {%- endfor %}
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
