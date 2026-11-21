-- Report only: `coffeedata build` runs it after the gate (tag report_only), so it never holds back a release. The
-- admin's Release page reads it from the draft build's ops tables (eval/prefilter/README.md).
{{ config(tags=['report_only']) }}
-- What a keyword filter change does to today's catalog, product by product: a census, not a sample.
--   excluded: a listed product this build's filters newly exclude (stg_products.prefilter_new): no rule of the code
--     excluded it and no filter of the served build did.
--   restored: a listed product a filter of the served build excluded and none of this build's does
--     (stg_products.prefilter_restored): it goes back to Claude and the rules.
-- Left out: products no store lists any more (is_listed false), and those an owner's decision settles (an include
-- keeps a product, an exclude already removed it).
-- claude: Claude's stored call on the version: coffee, not_coffee, or no_verdict (none yet, or unsure). An excluded
-- product Claude calls coffee or has no verdict on is one the owner reviews before an over-50 Publish.
-- is_coffee_after: the build's answer with this change (a restored product comes back to the catalog when true).
select
    if(p.prefilter_new, 'excluded', 'restored') as direction,
    d.coffee_id, d.roaster_id, d.title, d.url, d.input_hash,
    if(p.prefilter_new, p.prefilter, p.baseline_prefilter) as prefilter,
    d.verdict_label,
    case d.verdict_label when 'coffee' then 'coffee' when 'not_coffee' then 'not_coffee' else 'no_verdict' end
        as claude,
    d.is_coffee as is_coffee_after
from {{ ref('product_decisions') }} d
join {{ ref('stg_products') }} p using (input_hash)
where (p.prefilter_new or p.prefilter_restored) and d.is_listed and d.reason not like 'decision:%'
