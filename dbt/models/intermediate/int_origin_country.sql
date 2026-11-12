-- Every origin spelling the lake holds, as captured (the rules' in stg_products, Claude's in stg_enrichments), mapped
-- to ISO 3166-1 by the origin_country_iso seed (macros/origin_iso.sql says how lists and unknown spellings resolve).
-- The captured values stay as they are; dim_coffee publishes the ISO short name and alpha-2 code from here.
with captured as (
    select origin_country as raw_value from {{ ref('stg_products') }}
    union
    select origin_country from {{ ref('stg_enrichments') }} where method = 'llm'
)
{%- set names = var('origin_names') %}
{%- if names not in ['iso', 'common'] %}
{{ exceptions.raise_compiler_error("var origin_names must be iso or common, not " ~ names) }}
{%- endif %}
-- display_name: what the site shows (var origin_names): the ISO short name, or with `common` the
-- origin_display_name seed's name where it has one.
select
    o.*,
    {% if names == 'common' %}coalesce(d.display_name, o.iso_short_name){% else %}o.iso_short_name{% endif %}
        as display_name
from ({{ origin_iso('captured', ref('origin_country_iso')) }}) o
left join {{ ref('origin_display_name') }} d on d.alpha2 = o.alpha2
