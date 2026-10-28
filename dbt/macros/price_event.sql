{#- The change one observation of a bag size shows against the one before it (fct_price_event), or null for none:
    its first observation is 'new', one after a run that didn't list it 'relisted', then a 'price' or an
    'availability' change. Reads prev_obs_run (the bag's previous observation), prev_run (the roaster's previous
    counted run), price_usd, prev_price, available and prev_available. -#}
{% macro price_event() -%}
case
    when prev_obs_run is null then 'new'
    when prev_obs_run is distinct from prev_run then 'relisted'
    when price_usd is distinct from prev_price then 'price'
    when available is distinct from prev_available then 'availability'
end
{%- endmacro %}
