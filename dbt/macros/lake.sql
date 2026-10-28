{#- Read every file of a lake table: each root in var('lake_roots') is a local dir or an hf:// dataset URL. -#}
{% macro lake(table) -%}
{%- set globs = {
    'snapshots': 'snapshots/*/*.parquet', 'bodies': 'bodies/*/*.parquet', 'fetches': 'fetches/*/*.parquet',
    'probes': 'probes/*/*.parquet', 'registry': 'registry/*/*.parquet', 'enrichments': 'enrichments/*/*/*.parquet',
    'llm_batches': 'llm_batches/*/*.parquet', 'verdicts': 'verdicts/*/*.parquet', 'shots': 'shots/*.parquet', 'equipment': 'equipment/*.parquet',
    'bags': 'bags/*.parquet', 'photos': 'photos/*.parquet',
    'run_overrides': 'run_overrides/*.parquet', 'builds': 'builds/*/*.parquet',
    'run_manifests': 'run_manifests/*/*.parquet', 'serving_log': 'serving_log/*.parquet'} -%}
read_parquet([
  {%- for root in var('lake_roots') -%}'{{ root }}/{{ globs[table] }}'{% if not loop.last %}, {% endif %}{%- endfor -%}
], union_by_name = true, hive_partitioning = false)
{%- endmacro %}
