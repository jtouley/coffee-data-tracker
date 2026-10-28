{#- Lineage on every table a release serves: which build produced the row and which weekly run that build checked.
    Set by `coffeedata build` (vars build_id and as_of_run); null in a plain `dbt build`. -#}
{% macro release_columns() -%}
nullif('{{ var("build_id") }}', '') as release_build_id,
cast(nullif('{{ var("as_of_run") }}', '') as date) as release_run_date
{%- endmacro %}
