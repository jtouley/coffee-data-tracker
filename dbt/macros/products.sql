{#- Whether a product version counts as coffee: the owner's decision (stg_snapshots.decision: include or exclude)
    when there is one, else the classifier's (stg_products.is_coffee). -#}
{% macro decided(decision, is_coffee) -%}
case {{ decision }} when 'include' then true when 'exclude' then false else {{ is_coffee }} end
{%- endmacro %}

{#- The product-version key over a snapshots row: everything the classifier and the extraction rules read
    (stg_products runs them once per value). Same expression as coffeedata.products.INPUT_HASH_SQL. -#}
{% macro product_input_hash(s) -%}
md5(to_json(struct_pack({{ s }}.title, {{ s }}.product_type, {{ s }}.tags, {{ s }}.body_hash,
                        grams := list_transform({{ s }}.variants, lambda v: v.grams))))
{%- endmacro %}
