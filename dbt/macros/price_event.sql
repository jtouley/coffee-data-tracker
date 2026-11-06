{#- The change one observation of a bag size shows against the one before it (fct_price_event), or null for none:
    its first observation is 'new', one after a complete run that didn't list it 'relisted' (the run fct_price_event
    marked it 'removed' at), then a 'price' or an 'availability' change. Reads prev_obs_run (the bag's previous
    observation, always a counted run), prev_complete_run (the roaster's previous counted run that wasn't partial),
    price_usd, is_usd (whether the price is in USD: a price in another currency is null and never a price change),
    prev_usd_run and prev_usd_price (the bag's previous USD observation, so a USD price is compared with the last
    USD price even across a run priced in another currency), available and prev_available. Without partial runs prev_complete_run is the previous
    counted run, so this is "prev_obs_run is distinct from prev_run". With only partial runs before it, nothing was
    removed, so nothing is relisted. -#}
{% macro price_event() -%}
case
    when prev_obs_run is null then 'new'
    when prev_obs_run < prev_complete_run then 'relisted'
    when is_usd and prev_usd_run is not null and price_usd is distinct from prev_usd_price then 'price'
    when available is distinct from prev_available then 'availability'
end
{%- endmacro %}
