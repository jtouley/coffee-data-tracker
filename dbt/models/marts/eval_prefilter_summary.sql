-- Report only (tag report_only): never holds back a release.
{{ config(tags=['report_only']) }}
-- eval_prefilter's plain counts, one row per direction (excluded, restored), always both: how many products, and
-- Claude's call on them. Counts of a census of today's catalog: no interval, nothing estimated
-- (eval/prefilter/README.md). For a restored product, back_in_catalog counts those that come back as coffee.
with directions as (
    select direction from (values ('excluded'), ('restored')) as t (direction)
)
select
    g.direction,
    count(e.coffee_id) as products,
    count(e.coffee_id) filter (where e.claude = 'coffee') as claude_coffee,
    count(e.coffee_id) filter (where e.claude = 'not_coffee') as claude_not_coffee,
    count(e.coffee_id) filter (where e.claude = 'no_verdict') as no_verdict,
    count(e.coffee_id) filter (where e.is_coffee_after) as back_in_catalog
from directions g
left join {{ ref('eval_prefilter') }} e using (direction)
group by g.direction
order by g.direction
