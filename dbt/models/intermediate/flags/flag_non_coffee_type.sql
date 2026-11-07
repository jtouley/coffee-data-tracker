-- Rule non_coffee_type (family listing; coffeedata.rules): the store's product type names a kind of product that
-- isn't coffee (params.types, each a whole word or phrase, singular or plural: "drink mix" matches "Drink Mixes",
-- "nuts" matches "Nut", not "Peanuts").
{%- set types = var('rules')['rules']['non_coffee_type']['params']['types'] %}
{%- set stems = [] %}
{%- for t in types %}{% do stems.append(t[:-1] if t.endswith('s') and not t.endswith('ss') else t) %}{% endfor %}
with m as (
    select coffee_id, roaster_id, product_type,
           regexp_extract(lower(product_type), '\b({{ stems | join("|") }})(e?s)?\b', 1) as matched
    from {{ ref('int_catalog_candidates') }}
    where relevant and product_type is not null
)
select coffee_id, roaster_id, '' as direction,
       to_json({'product_type': product_type, 'matched': matched}) as evidence, matched as evidence_key
from m where matched <> ''
