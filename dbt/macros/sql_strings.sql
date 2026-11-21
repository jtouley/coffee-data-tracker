-- A list of strings as SQL literals, comma-separated, quotes doubled: the domain lists cli._build passes as vars
-- (source_roasters, not_roasters) for stg_registry's `in (...)`.
{% macro sql_strings(values) -%}
{%- for v in values %}'{{ v | replace("'", "''") }}'{{ ", " if not loop.last }}{% endfor -%}
{%- endmacro %}
