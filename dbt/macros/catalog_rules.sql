{#- The catalog's two definitions, written once (the site's site.LISTED and site.SINGLE_ORIGIN say the same over
    site_coffee; tests/test_dbt.py checks the site against public_market_weekly):
    - current: listed now, not the roaster's archived back catalog (int_current_catalog);
    - single_origin: current, retail (not wholesale), not a blend, origin known: what public_market_weekly counts
      and release_changes reconciles it on. `alias`: the relation's alias, if the columns need one. -#}
{% macro is_current(alias='') -%}
{%- set a = alias ~ '.' if alias else '' -%}
({{ a }}is_listed and {{ a }}catalog_status <> 'archived')
{%- endmacro %}

{% macro is_single_origin(alias='') -%}
{%- set a = alias ~ '.' if alias else '' -%}
({{ is_current(alias) }} and not {{ a }}is_wholesale and not {{ a }}is_blend and {{ a }}origin_country is not null)
{%- endmacro %}
