{#- Every raw file of the importers dataset: var('importer_sources'), the globs Lake.sources("importer_raw") gives. -#}
{% macro importer_raw() -%}
{%- set paths = [] -%}
{%- for p in var('importer_sources') -%}
  {%- do paths.append("'" ~ p ~ "'") -%}
{%- endfor -%}
read_parquet([{{ paths | join(', ') }}], union_by_name = true, hive_partitioning = false)
{%- endmacro %}
