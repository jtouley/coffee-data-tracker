{#- The grain test for a compound key (docs/dbt/CONVENTION.md, section 5): `columns` together identify one row.
    It fails with one row per key that more than one row has, and one row per row with a null key column.

    Each column is compared on its own, and a null is a value like any other (group by puts nulls together). So two
    rows that share a null part are a duplicate, and a null part is itself a failure unless the column is listed in
    `nullable` (a part of the key that is legitimately unknown: mart_dial_in's grinder_id). `unique` over
    `a || '|' || b`, which this replaces, saw neither: a null part makes the whole expression null, and `unique`
    skips nulls. It also read ('a|b', 'c') and ('a', 'b|c') as one key.

    A single-column key keeps `unique` and `not_null` on the column. -#}
{% test grain(model, columns, nullable=[]) %}
{%- if columns is string or columns | length < 2 -%}
  {{ exceptions.raise_compiler_error("grain: `columns` is a list of two or more columns; a single key takes unique and not_null") }}
{%- endif -%}
{%- for c in nullable if c not in columns -%}
  {{ exceptions.raise_compiler_error("grain: nullable column `" ~ c ~ "` is not one of `columns`") }}
{%- endfor -%}
{%- set key = columns | join(', ') %}
select {{ key }}, count(*) as key_rows, 'duplicate key' as problem
from {{ model }}
group by {{ key }}
having count(*) > 1
{%- for c in columns if c not in nullable %}
union all
select {{ key }}, 1 as key_rows, 'null {{ c }}' as problem
from {{ model }}
where {{ c }} is null
{%- endfor %}
{% endtest %}
