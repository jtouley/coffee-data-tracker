-- A coffee_id (roaster_id:product_id) under its store's canonical roaster id. dim_coffee leaves an alias's coffees
-- out (its products are the canonical id's own, the same product ids), so a shot or a bag logged against an alias's
-- coffee resolves to the canonical id's twin; any other coffee_id is returned as it is. That holds for a host group
-- (one site, one platform). A hand-set alias_of onto a different site (a rebrand that doesn't redirect) has other
-- product ids: its coffees leave dim_coffee and a shot's coffee_id here may name no coffee (fct_shot warns), so set
-- alias_of only once the old site's catalog is the new one's.
{% macro canonical_coffee_id(coffee_id) -%}
coalesce((
    select a.canonical_roaster_id || ':' || substr({{ coffee_id }}, length(a.roaster_id) + 2)
    from {{ ref('int_roaster_state') }} a
    where a.roaster_id <> a.canonical_roaster_id and a.roaster_id = split_part({{ coffee_id }}, ':', 1)
), {{ coffee_id }})
{%- endmacro %}
