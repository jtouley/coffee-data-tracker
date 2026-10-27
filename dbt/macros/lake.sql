{#- Read every file of a lake table: each root in var('lake_roots') is a local dir or an hf:// dataset URL. -#}
{% macro lake(table) -%}
{%- set globs = {
    'snapshots': 'snapshots/*/*.parquet', 'bodies': 'bodies/*/*.parquet', 'fetches': 'fetches/*/*.parquet',
    'probes': 'probes/*/*.parquet', 'registry': 'registry/*/*.parquet', 'enrichments': 'enrichments/*/*/*.parquet',
    'llm_batches': 'llm_batches/*/*.parquet', 'shots': 'shots/*.parquet'} -%}
read_parquet([
  {%- for root in var('lake_roots') -%}'{{ root }}/{{ globs[table] }}'{% if not loop.last %}, {% endif %}{%- endfor -%}
], union_by_name = true, hive_partitioning = false)
{%- endmacro %}
