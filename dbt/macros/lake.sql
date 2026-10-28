{#- Read every file of a lake table: each root in var('lake_roots') is a local dir or an hf:// dataset URL. -#}
{% macro lake(table) -%}
{%- set globs = {
    'snapshots': 'snapshots/*/*.parquet', 'bodies': 'bodies/*/*.parquet', 'fetches': 'fetches/*/*.parquet',
    'probes': 'probes/*/*.parquet', 'registry': 'registry/*/*.parquet', 'enrichments': 'enrichments/*/*/*.parquet',
    'llm_batches': 'llm_batches/*/*.parquet', 'verdicts': 'verdicts/*/*.parquet', 'shots': 'shots/*.parquet', 'equipment': 'equipment/*.parquet',
    'bags': 'bags/*.parquet', 'photos': 'photos/*.parquet',
    'run_overrides': 'run_overrides/*.parquet', 'builds': 'builds/*/*.parquet',
    'run_manifests': 'run_manifests/*/*.parquet', 'serving_log': 'serving_log/*.parquet'} -%}
{#- A remote root is read only for folders it has (var('remote_folders'); none: all of them), since DuckDB fails on a
    glob over a Hugging Face folder that doesn't exist yet. The local root always has the table's schema file. -#}
{%- set folders = var('remote_folders', none) -%}
{%- set paths = [] -%}
{%- for root in var('lake_roots') -%}
  {%- if not root.startswith('hf://') or folders is none or globs[table].split('/')[0] in folders -%}
    {%- do paths.append("'" ~ root ~ "/" ~ globs[table] ~ "'") -%}
  {%- endif -%}
{%- endfor -%}
read_parquet([{{ paths | join(', ') }}], union_by_name = true, hive_partitioning = false)
{%- endmacro %}
