-- Current attributes per (product, description): Claude's extraction beats the rules; newest wins. Two extractions in
-- the same second: the one whose row hashes first, so the pick never depends on the order rows are read.
select * exclude (rn)
from (
    select *, row_number() over (
        partition by roaster_id, product_id, body_hash
        order by (method = 'llm') desc, extracted_at desc, md5(to_json(e))) as rn
    from {{ lake('enrichments') }} e
    where roaster_id is not null
)
where rn = 1
