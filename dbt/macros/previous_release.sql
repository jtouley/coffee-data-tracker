{#- One public table of the previously served release, or none of its rows when nothing was served.
    var previous_release is {table: {"path": <a local copy of the served file>, "columns": [<its columns>]}}, set by
    `coffeedata build` (quality.stage_previous_release), which copies the served files first, so a failed read of
    the lake never reaches dbt. `columns` are the ones the reconciliation compares: one the served file lacks (a
    release written before the column was added) reads as a typed null, and release_changes reports it (change
    'schema'), so an older release still reconciles on the rest. hive_partitioning is off: the lake keeps a release
    under releases/run_date=<run>/build=<id>/, and a hive read would replace public_price_event's own run_date. -#}
{% macro previous_release(table, columns) -%}
{%- set spec = (var('previous_release', {}) or {}).get(table) -%}
{%- if spec -%}
{%- set have = spec.get('columns') or [] -%}
{%- set types = {} -%}
{%- if execute -%}
  {%- for c in adapter.get_columns_in_relation(ref(table)) -%}{%- do types.update({c.name: c.dtype}) -%}{%- endfor -%}
{%- endif -%}
select {% for c in columns -%}
  {%- if c in have %}{{ c }}{% else %}cast(null as {{ types.get(c, 'varchar') }}) as {{ c }}{% endif %}
  {{- ', ' if not loop.last }}
{%- endfor %}
from read_parquet('{{ spec.path }}', hive_partitioning = false)
{%- else -%}
select {{ columns | join(', ') }} from {{ ref(table) }} where false
{%- endif -%}
{%- endmacro %}

{#- The columns release_changes compares that the served `table` lacks (see previous_release). -#}
{% macro previous_release_missing(table, columns) -%}
{%- set spec = (var('previous_release', {}) or {}).get(table) -%}
{{ return([] if not spec else columns | reject('in', spec.get('columns') or []) | list) }}
{%- endmacro %}

{#- A hash of a row's `columns` (nulls kept distinct from any value), to compare one row across two releases. -#}
{% macro row_hash(alias, columns) -%}
md5(concat_ws('|', {% for c in columns %}coalesce(cast({{ alias }}.{{ c }} as varchar), '<null>'){{ ', ' if not loop.last }}{% endfor %}))
{%- endmacro %}

{#- The local copy of one of the served release's public tables, when it is there to read, else none. The rules
    (int_catalog_candidates, flag_verdict_flip, mart_flags) run in the gate, unlike release_changes: a served copy
    that is missing must never fail a build, so they read none and raise no flag that needs it (the guard then
    holds nothing, and the build is the classifier's alone). -#}
{% macro served_file(table) -%}
{%- set spec = (var('previous_release', {}) or {}).get(table) -%}
{%- if not spec or not execute -%}{{ return(spec.path if spec else none) }}{%- endif -%}
{%- set found = run_query("select count(*) from glob('" ~ spec.path | replace("'", "''") ~ "')") -%}
{{ return(spec.path if found.columns[0].values()[0] > 0 else none) }}
{%- endmacro %}
