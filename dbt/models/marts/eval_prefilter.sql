-- Report only: `coffeedata build` runs it after the gate (tag report_only), so it never holds back a release. The
-- admin's Publish reads it from the draft build's ops tables (eval/prefilter/README.md).
{{ config(tags=['report_only']) }}
-- The evaluation of a keyword filter change, in the draft build that carries it (eval/prefilter/README.md):
--   population: every product, as of its latest record, that this build's keyword filters newly exclude
--     (stg_products.prefilter_new: no rule of the code excluded it, and no filter of the served build did), less
--     those an owner's decision already settles (an include keeps it, an exclude already removed it). The whole
--     population, not a sample: there is one frozen candidate and nothing to tune, so no split is drawn.
--   reference: Claude's stored verdict for the version (CLAUDE.md step 6: an existing signal can be the reference
--     when the change isn't that model or prompt; a keyword filter isn't).
--   agreement: Claude also says not_coffee. Claude says coffee: a confidently wrong candidate, listed for the owner.
--     No verdict, or unsure: unknown, counted apart and never as agreement (the share's denominator keeps them).
-- One summary row (kind = 'summary', always present, zeros when nothing is newly excluded), then one row per
-- affected product (kind = 'product'). share_not_coffee carries a Wilson 95% interval (z = 1.96), each rounded to six places.
with affected as (
    select d.coffee_id, d.roaster_id, d.title, d.url, d.input_hash, p.prefilter, d.verdict_label
    from {{ ref('product_decisions') }} d
    join {{ ref('stg_products') }} p using (input_hash)
    where p.prefilter_new and d.reason not like 'decision:%'
),
counts as (
    select count(*) as affected,
           count(*) filter (where verdict_label = 'not_coffee') as claude_not_coffee,
           count(*) filter (where verdict_label = 'coffee') as claude_coffee,
           count(*) filter (where verdict_label is null or verdict_label not in ('coffee', 'not_coffee'))
               as no_verdict
    from affected
),
wilson as (
    select *,
           case when affected > 0 then claude_not_coffee / affected end as p,
           1.96 * 1.96 / nullif(affected, 0) as z2n
    from counts
)
select 'summary' as kind, null::varchar as coffee_id, null::varchar as roaster_id, null::varchar as title,
       null::varchar as url, null::varchar as input_hash, null::varchar as prefilter, null::varchar as verdict_label,
       affected, claude_not_coffee, claude_coffee, no_verdict, round(p, 6) as share_not_coffee,
       round((p + z2n / 2 - 1.96 * sqrt(p * (1 - p) / affected + z2n / (4 * affected))) / (1 + z2n), 6) as wilson_low,
       round((p + z2n / 2 + 1.96 * sqrt(p * (1 - p) / affected + z2n / (4 * affected))) / (1 + z2n), 6) as wilson_high
from wilson
union all
select 'product', coffee_id, roaster_id, title, url, input_hash, prefilter, verdict_label,
       null, null, null, null, null, null, null
from affected
