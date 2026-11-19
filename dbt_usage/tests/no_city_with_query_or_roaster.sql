-- City is never shown with search text, a single roaster's or coffee's clicks, rank, a campaign tag or two facets
-- at once (design section 5). Checked by meaning, not by a column's name: every mart's columns are listed here, the
-- visitor's place may appear only in the place marts, and a new or renamed column (a `place_city` beside a roaster)
-- fails until it is added to this list on review. A facet is one of the allowed ones. Returns one row per breach.
-- depends_on: {{ ref('mart_usage_locations_weekly') }}, {{ ref('mart_usage_city_facets_weekly') }}
-- depends_on: {{ ref('mart_roaster_clicks_weekly') }}, {{ ref('mart_coffee_clicks_weekly') }}
-- depends_on: {{ ref('mart_click_context_weekly') }}, {{ ref('mart_usage_search_terms_weekly') }}
-- depends_on: {{ ref('mart_usage_sources_weekly') }}, {{ ref('mart_usage_places_weekly') }}
-- depends_on: {{ ref('mart_usage_filter_combos_weekly') }}, {{ ref('mart_usage_page_views_weekly') }}
{{ config(severity='error') }}
{%- set allowed = {
    'mart_usage_locations_weekly': ['week', 'country', 'region', 'city', 'events', 'page_views'],
    'mart_usage_city_facets_weekly': ['week', 'country', 'region', 'city', 'facet', 'facet_value', 'events'],
    'mart_roaster_clicks_weekly': ['week', 'roaster_id', 'surface', 'device', 'source', 'country', 'region',
                                   'click_outs'],
    'mart_coffee_clicks_weekly': ['week', 'coffee_id', 'roaster_id', 'click_outs', 'median_rank'],
    'mart_click_context_weekly': ['week', 'roaster_id', 'context_kind', 'context_value', 'click_outs'],
    'mart_usage_search_terms_weekly': ['week', 'query', 'searches', 'zero_result_searches'],
    'mart_usage_sources_weekly': ['week', 'source_kind', 'source', 'utm_medium', 'utm_campaign', 'landings'],
    'mart_usage_places_weekly': ['week', 'place_type', 'place', 'searches'],
    'mart_usage_filter_combos_weekly': ['week', 'origin', 'process', 'state', 'in_stock', 'sort', 'result_views',
                                        'zero_result_views'],
    'mart_usage_page_views_weekly': ['week', 'surface', 'device', 'page_views'],
} %}
{%- set place_marts = ['mart_usage_locations_weekly', 'mart_usage_city_facets_weekly'] %}
{%- set pairs = [] %}
{%- for table, columns in allowed.items() %}{% for c in columns %}{% do pairs.append((table, c)) %}{% endfor %}{% endfor %}
with allowed as (
    select * from (values
    {%- for table, c in pairs %}
        ('{{ table }}', '{{ c }}'){{ ',' if not loop.last }}
    {%- endfor %}
    ) as t(table_name, column_name)
),

marts as (
    select table_name, column_name from information_schema.columns
    where table_name like 'mart\_%' escape '\'
)

-- a column no list names, in a listed mart or in a mart no list knows
select m.table_name, 'unlisted column ' || m.column_name as breach
from marts as m
left join allowed as a on a.table_name = m.table_name and a.column_name = m.column_name
where a.column_name is null
union all
-- the visitor's city anywhere but the place marts (belt and braces over the list)
select table_name, 'city in ' || table_name
from marts
where (column_name ilike '%city%' or column_name ilike '%town%')
  and table_name not in ('{{ place_marts | join("', '") }}')
union all
select 'mart_usage_city_facets_weekly', 'facet ' || facet
from {{ ref('mart_usage_city_facets_weekly') }}
where facet not in ('surface', 'device', 'origin', 'process', 'in_stock', 'sort', 'source')
