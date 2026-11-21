-- Rule shared_body (family catalog; coffeedata.rules): the description, or a paragraph of it of at least min_chars
-- characters (lowercased, spaces collapsed), is on min_products or more of the roaster's products (their latest
-- records, coffee or not): pasted text, such as a sample pack's paragraph on each coffee it holds. The flag names
-- the most shared such text; evidence_key is its hash, so the products sharing it group together.
{%- set p = var('rules')['rules']['shared_body']['params'] %}
with texts as (
    select c.coffee_id, c.roaster_id, c.relevant, t.text
    from {{ ref('int_catalog_candidates') }} c
    join {{ ref('stg_product_inputs') }} i using (input_hash),
    unnest(list_distinct(list_transform(list_append(string_split(coalesce(i.body_text, ''), chr(10)),
                                                    coalesce(i.body_text, '')),
                                        lambda x: lower(regexp_replace(trim(x), '\s+', ' ', 'g'))))) as t(text)
    where length(t.text) >= {{ p['min_chars'] }}
),
shared as (
    select roaster_id, text, count(distinct coffee_id) as products from texts group by all
    having count(distinct coffee_id) >= {{ p['min_products'] }}
)
select t.coffee_id, t.roaster_id, '' as direction,
       to_json({'products': s.products, 'text': left(s.text, 300)}) as evidence, md5(s.text) as evidence_key
from texts t join shared s using (roaster_id, text)
where t.relevant
qualify row_number() over (partition by t.coffee_id order by s.products desc, md5(s.text)) = 1
