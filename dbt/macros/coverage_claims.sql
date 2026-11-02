{#- The coverage claims in marketplace/LISTING.md (var coverage_claims, which `coffeedata build` parses from it with
    quality.coverage_claims), as rows: source (the file and line to update), metric, claimed, text. -#}
{% macro coverage_claims() -%}
    {%- set claims = var('coverage_claims') -%}
    {%- for c in claims %}
    select '{{ c["source"] }}' as source, '{{ c["metric"] }}' as metric, {{ c["value"] }}::double as claimed,
           '{{ c["text"] | replace("'", "''") }}' as text
    {%- if not loop.last %} union all{% endif %}
    {%- endfor %}
    {%- if not claims %}
    select null::varchar as source, null::varchar as metric, null::double as claimed, null::varchar as text
    where false
    {%- endif %}
{%- endmacro %}
