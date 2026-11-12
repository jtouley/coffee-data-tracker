{#- Small pieces the rules share. Each takes SQL expressions and returns one. -#}

{#- Text without whitespace at either end, in any script (trim() only takes spaces). -#}
{% macro strip(text) -%}
regexp_replace({{ text }}, '^\s+|\s+$', '', 'g')
{%- endmacro %}

{#- A text's non-empty lines, each stripped, in order; [] for null. -#}
{% macro lines(text) -%}
list_filter(list_transform(string_split(coalesce({{ text }}, ''), chr(10)), lambda x: {{ strip('x') }}), lambda x: x != '')
{%- endmacro %}

{#- The one value a list of stated values agrees on, ignoring case, spacing and blanks (the last spelling given);
    null when it holds none, or two that differ. -#}
{% macro one_value(values) -%}
(case
    when len(list_distinct(list_transform(
        list_filter({{ values }}, lambda v: {{ strip('v') }} != ''),
        lambda v: lower(regexp_replace({{ strip('v') }}, '\s+', ' ', 'g'))
    ))) = 1
    then {{ strip("list_filter(" ~ values ~ ", lambda v: " ~ strip('v') ~ " != '')[-1]") }}
end)
{%- endmacro %}

{#- One process in the catalog's terms from a text: anaerobic wins; honey and its relatives (the `honey` pattern)
    before natural and washed, whose words they contain; a text naming none, or two kinds, is null. -#}
{% macro process_kind(text, anaerobic, honey, natural, washed) -%}
(case
    when regexp_matches({{ text }}, {{ anaerobic }}, 'i') then 'anaerobic'
    when regexp_matches({{ text }}, {{ honey }}, 'i')::int
        + regexp_matches(regexp_replace({{ text }}, {{ honey }}, ' ', 'gi'), {{ natural }}, 'i')::int
        + regexp_matches(regexp_replace({{ text }}, {{ honey }}, ' ', 'gi'), {{ washed }}, 'i')::int != 1 then null
    when regexp_matches({{ text }}, {{ honey }}, 'i') then 'honey'
    when regexp_matches(regexp_replace({{ text }}, {{ honey }}, ' ', 'gi'), {{ natural }}, 'i') then 'natural'
    else 'washed'
end)
{%- endmacro %}

{#- The months from month index a to b (0-11), wrapping past December, as three-letter names. -#}
{% macro month_range(a, b) -%}
list_transform(
    range(((({{ b }}) - ({{ a }})) % 12 + 12) % 12 + 1),
    lambda i: ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'][(({{ a }}) + i) % 12 + 1]
)
{%- endmacro %}

{#- A month word's index, 0-11 ("Sept" and "September" are 8); null for anything else. -#}
{% macro month_index(word) -%}
(list_position(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], lower(nullif({{ word }}, ''))[1:3]) - 1)
{%- endmacro %}
