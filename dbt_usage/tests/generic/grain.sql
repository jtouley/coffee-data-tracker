{#- The grain test for a compound key (docs/dbt/CONVENTION.md, section 5): `columns` together identify one row.
    It fails with one row per key that has a problem: more than one row has it, or a key column is null.

    Each column is compared on its own, and a null is a value like any other (group by puts nulls together). So two
    rows that share a null part are a duplicate, and a null part is itself a failure unless the column is listed in
    `nullable` (a part of the key that is legitimately unknown: mart_dial_in's grinder_id). `unique` over
    `a || '|' || b`, which this replaces, saw neither: a null part makes the whole expression null, and `unique`
    skips nulls. It also read ('a|b', 'c') and ('a', 'b|c') as one key. One pass over the model.

    A single-column key keeps `unique` and `not_null` on the column. -#}
{% test grain(model, columns, nullable=[]) %}
{%- if columns is string or columns | length < 2 -%}
  {{ exceptions.raise_compiler_error("grain: `columns` is a list of two or more columns; a single key takes unique and not_null") }}
{%- endif -%}
{%- for c in nullable if c not in columns -%}
  {{ exceptions.raise_compiler_error("grain: nullable column `" ~ c ~ "` is not one of `columns`") }}
{%- endfor -%}
{%- set required = columns | reject('in', nullable) | list -%}
{%- set key = columns | join(', ') %}
select {{ key }}, count(*) as key_rows,
    concat_ws(', ', case when count(*) > 1 then 'duplicate key' end
    {%- for c in required %}, case when {{ c }} is null then 'null {{ c }}' end{% endfor %}) as problem
from {{ model }}
group by {{ key }}
having count(*) > 1
{%- for c in required %} or {{ c }} is null{% endfor %}
{% endtest %}
