{#- Everything the classifier and the extraction rules read, hashed: one product version (verdict.INPUT_HASH in
    Python). stg_products runs them once per distinct value. -#}
{% macro input_hash(s) -%}
md5(to_json(struct_pack({{ s }}.title, {{ s }}.product_type, {{ s }}.tags, {{ s }}.body_hash,
                        grams := list_transform({{ s }}.variants, lambda v: v.grams))))
{%- endmacro %}
