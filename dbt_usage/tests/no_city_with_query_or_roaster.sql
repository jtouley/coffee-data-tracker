-- City is never shown with search text, a single roaster's or coffee's clicks, rank, a campaign tag or two facets
-- at once (design section 5). A usage table with a city column may hold only the week, country, region, city, one
-- facet (facet, facet_value) and counts; and a facet is one of the allowed ones. Returns one row per breach.
-- depends_on: {{ ref('mart_usage_locations_weekly') }}, {{ ref('mart_usage_city_facets_weekly') }}
-- depends_on: {{ ref('mart_roaster_clicks_weekly') }}, {{ ref('mart_coffee_clicks_weekly') }}
-- depends_on: {{ ref('mart_click_context_weekly') }}, {{ ref('mart_usage_search_terms_weekly') }}
-- depends_on: {{ ref('mart_usage_sources_weekly') }}, {{ ref('mart_usage_places_weekly') }}
-- depends_on: {{ ref('mart_usage_filter_combos_weekly') }}, {{ ref('mart_usage_page_views_weekly') }}
{{ config(severity='error') }}
with with_city as (
    select distinct table_name from information_schema.columns
    where column_name = 'city' and table_name like 'mart\_%' escape '\'
)

select c.table_name, c.column_name as breach
from information_schema.columns as c
inner join with_city as t on t.table_name = c.table_name
where c.column_name not in ('week', 'country', 'region', 'city', 'facet', 'facet_value', 'events', 'page_views')
union all
select 'mart_usage_city_facets_weekly', 'facet ' || facet
from {{ ref('mart_usage_city_facets_weekly') }}
where facet not in ('surface', 'device', 'origin', 'process', 'in_stock', 'sort', 'source')
