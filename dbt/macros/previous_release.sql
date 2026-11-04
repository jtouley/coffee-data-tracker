{#- One public table of the previously served release (var previous_release: {table: parquet path}, set by
    `coffeedata build` from the served release's files), or none of its rows when nothing was served. `columns` are
    the ones read, so a release written before a column was added still reads. hive_partitioning is off: the files
    sit under releases/run_date=<run>/build=<id>/, and a hive read would replace public_price_event's own run_date
    with the release's. -#}
{% macro previous_release(table, columns) -%}
{%- set path = (var('previous_release', {}) or {}).get(table) -%}
{%- if path -%}
select {{ columns | join(', ') }} from read_parquet('{{ path }}', hive_partitioning = false, union_by_name = true)
{%- else -%}
select {{ columns | join(', ') }} from {{ ref(table) }} where false
{%- endif -%}
{%- endmacro %}

{#- A hash of a row's `columns` (nulls kept distinct from any value), to compare one row across two releases. -#}
{% macro row_hash(alias, columns) -%}
md5(concat_ws('|', {% for c in columns %}coalesce(cast({{ alias }}.{{ c }} as varchar), '<null>'){{ ', ' if not loop.last }}{% endfor %}))
{%- endmacro %}
