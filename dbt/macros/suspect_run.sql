{#- Whether a roaster-week's catalog looks broken: its product count falls below suspect_drop_ratio of the median of
    `history` (the product counts of the roaster's last 4 counted weeks), when that median is at least
    suspect_min_products. With no history there is no baseline, so a week is never suspect.
    TODO: follow-up after ~8 weeks of history: replace the fixed 'below half the median of the last 4 counted
    weeks' suspect rule with per-roaster MAD bands (both sides, warn-only, active once a roaster has >=6 counted
    weeks, reset on accept/override), same pattern as the catalog bands (tests/build_metrics_in_band.sql). -#}
{% macro suspect_run(products, history) -%}
coalesce(
    list_aggregate({{ history }}, 'median') >= {{ var('suspect_min_products') }}
    and {{ products }} < {{ var('suspect_drop_ratio') }} * list_aggregate({{ history }}, 'median'),
    false
)
{%- endmacro %}
