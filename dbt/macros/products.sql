{#- The product-version key over a snapshots row: everything the classifier and the extraction rules read
    (stg_products runs them once per value). Same expression as coffeedata.products.INPUT_HASH_SQL. -#}
{% macro product_input_hash(s) -%}
md5(to_json(struct_pack({{ s }}.title, {{ s }}.product_type, {{ s }}.tags, {{ s }}.body_hash,
                        grams := list_transform({{ s }}.variants, lambda v: v.grams))))
{%- endmacro %}
