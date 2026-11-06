{#- Whether a price is publishable as US dollars: the store states USD (as an ISO 4217 code, any case or spacing),
    or states no currency and the roaster is in the US. Anything else is false, never null. -#}
{% macro is_usd(currency, country) -%}
coalesce(upper(trim({{ currency }})) = 'USD', {{ country }} = 'US', false)
{%- endmacro %}
