-- Rule non_coffee_type (family listing; coffeedata.rules): the store's product type names a kind of product that
-- isn't coffee (params.types, each a whole word or phrase, singular or plural: "drink mix" matches "Drink Mixes",
-- "nuts" matches "Nut", "glasses" matches "Glass"; "nuts" doesn't match "Peanuts"). evidence_key is the listed type.
{%- set types = var('rules')['rules']['non_coffee_type']['params']['types'] %}
with m as (
    select coffee_id, roaster_id, product_type,
           case
           {%- for t in types %}
           {%- set forms = [t] + ([t[:-2]] if t.endswith('es') else []) + ([t[:-1]] if t.endswith('s') and not t.endswith('ss') else []) %}
               when regexp_matches(lower(product_type), '\b({{ forms | join("|") }})(e?s)?\b') then '{{ t }}'
           {%- endfor %}
           end as matched
    from {{ ref('int_catalog_candidates') }}
    where relevant and product_type is not null
)
select coffee_id, roaster_id, '' as direction,
       to_json({'product_type': product_type, 'matched': matched}) as evidence, matched as evidence_key
from m where matched is not null
