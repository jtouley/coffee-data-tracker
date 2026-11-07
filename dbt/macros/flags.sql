{#- Every rule's flags (dbt/models/intermediate/flags/flag_<id>.sql, one per rule type in coffeedata.rules), with
    the rule, its family and its polarity from the settings (var rules), one row per flag. -#}
{% macro all_flags() -%}
{%- set rules = var('rules')['rules'] %}
{%- for id, r in rules.items() %}
select '{{ id }}' as rule, '{{ r['family'] }}' as family,
       case direction {% for d, pol in r['polarity'].items() %}when '{{ d }}' then '{{ pol }}' {% endfor %}end
           as polarity,
       direction, coffee_id, roaster_id, evidence, evidence_key
from {{ ref('flag_' ~ id) }}
{{ 'union all' if not loop.last }}
{%- endfor %}
{%- if not rules %}
select null::varchar as rule, null::varchar as family, null::varchar as polarity, null::varchar as direction,
       null::varchar as coffee_id, null::varchar as roaster_id, null::varchar as evidence,
       null::varchar as evidence_key where false
{%- endif %}
{%- endmacro %}
