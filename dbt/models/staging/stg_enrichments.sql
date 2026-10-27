-- Current attributes per (product, description): Claude's extraction beats the rules; newest wins.
select * exclude (rn)
from (
    select *, row_number() over (
        partition by roaster_id, product_id, body_hash
        order by (method = 'llm') desc, extracted_at desc) as rn
    from {{ lake('enrichments') }}
    where roaster_id is not null
)
where rn = 1
