{#- Whether a roaster-week's catalog looks broken: its product count falls below suspect_drop_ratio of the median of
    `history` (the product counts of the roaster's last 4 counted weeks), when that median is at least
    suspect_min_products. With no history there is no baseline, so a week is never suspect. -#}
{% macro suspect_run(products, history) -%}
coalesce(
    list_aggregate({{ history }}, 'median') >= {{ var('suspect_min_products') }}
    and {{ products }} < {{ var('suspect_drop_ratio') }} * list_aggregate({{ history }}, 'median'),
    false
)
{%- endmacro %}
