{#- The raw event row (lake.SCHEMAS['usage_events'], contract usage 1.0.0, design section 0): its columns and types, also when there are no files yet. Raw files are read
    by name over this empty relation, so a column a file lacks is null and a build with no events still builds. -#}
{% macro usage_raw_columns() -%}
select
    null::varchar as event_id, null::timestamp as received_at, null::varchar as event, null::varchar as surface,
    null::varchar as release, null::varchar as device, null::varchar as query, null::varchar[] as cities,
    null::varchar[] as states, null::varchar as origin, null::varchar as process, null::varchar as state,
    null::boolean as in_stock, null::varchar as sort, null::integer as results_n, null::boolean as zero_results,
    null::varchar as roaster_id, null::varchar as coffee_id, null::integer as rank, null::varchar as source, null::varchar as referrer_domain,
    null::varchar as utm_source, null::varchar as utm_medium, null::varchar as utm_campaign,
    null::varchar as ip_prefix, null::boolean as bot, null::varchar as filename
where false
{%- endmacro %}

{#- Every raw event file: var('usage_sources'), the globs usage_build.sources() gives (events/day=*/batch=*.parquet
    under the staged root, and the Hub's when it is remote). Read only in staging. -#}
{% macro usage_raw() -%}
{%- set paths = [] -%}
{%- for p in var('usage_sources') -%}
  {%- do paths.append("'" ~ p ~ "'") -%}
{%- endfor -%}
({{ usage_raw_columns() }}
{%- if paths %}
union all by name
select * from read_parquet([{{ paths | join(', ') }}], union_by_name = true, hive_partitioning = false, filename = true)
{%- endif %})
{%- endmacro %}

{#- One pinned geo file (var('geo_db') or var('geo_cities')), verified against config/geo.lock before dbt runs, or an
    empty relation of its columns when the build has none. -#}
{% macro geo_file(name, columns) -%}
{%- set path = var(name) -%}
{%- if path %}
(select {{ columns | join(', ') }} from read_parquet('{{ path }}'))
{%- else %}
(select {% for c in columns %}null::varchar as {{ c }}{{ ', ' if not loop.last }}{% endfor %} where false)
{%- endif %}
{%- endmacro %}

{#- A served-release table (var('catalog_roasters') or var('catalog_coffees')), or nothing. -#}
{% macro catalog_file(name, columns) -%}
{{ geo_file(name, columns) }}
{%- endmacro %}

{#- An address's 128 bits as 32 lowercase hex digits, the order the geo range table sorts in; IPv4 is IPv4-mapped
    (::ffff:a.b.c.d), as src/coffeedata/geo.py writes the table. `addr` is an address without its /length. Null for
    text that isn't an address. -#}
{% macro ip_to_hex(addr) -%}
case
    when regexp_full_match({{ addr }}, '[0-9]{1,3}(\.[0-9]{1,3}){3}')
         and list_bool_and(list_transform(string_split({{ addr }}, '.'), lambda x: cast(x as integer) <= 255))
        then '00000000000000000000ffff'
             || array_to_string(list_transform(string_split({{ addr }}, '.'),
                                               lambda x: lpad(lower(to_hex(cast(x as integer))), 2, '0')), '')
    when regexp_full_match({{ addr }}, '[0-9A-Fa-f:]+') and len(string_split({{ addr }}, '::')) <= 2
        then list_transform([
            case when contains({{ addr }}, '::') then list_concat(
                list_filter(string_split(split_part({{ addr }}, '::', 1), ':'), lambda g: g <> ''),
                list_transform(range(greatest(0, 8
                    - len(list_filter(string_split(split_part({{ addr }}, '::', 1), ':'), lambda g: g <> ''))
                    - len(list_filter(string_split(split_part({{ addr }}, '::', 2), ':'), lambda g: g <> '')))), lambda x: '0'),
                list_filter(string_split(split_part({{ addr }}, '::', 2), ':'), lambda g: g <> ''))
            else string_split({{ addr }}, ':') end
        ], lambda gs: case
            when len(gs) = 8 and list_bool_and(list_transform(gs, lambda g: length(g) between 1 and 4))
                then array_to_string(list_transform(gs, lambda g: lpad(lower(g), 4, '0')), '')
        end)[1]
end
{%- endmacro %}

{#- The days each key occurred on in the usage_window_days days ending on each week's last day (design section 3),
    for every week in `rel` that has the key: one row per (week, keys) with `days`. `rel` holds week, received_date
    and the key columns. -#}
{% macro days_in_window(rel, keys) -%}
(
    select w.week, {% for k in keys %}d.{{ k }}, {% endfor %}count(distinct d.received_date) as days
    from (select distinct week, {{ keys | join(', ') }} from {{ rel }}) as w
    inner join (select distinct received_date, {{ keys | join(', ') }} from {{ rel }}) as d
        on {% for k in keys %}w.{{ k }} is not distinct from d.{{ k }} and {% endfor %}
           d.received_date between w.week + 6 - ({{ var('usage_window_days') }} - 1) and w.week + 6
    group by all
)
{%- endmacro %}
