-- Report only: `coffeedata build` runs it after the gate (tag report_only), so it can never hold back a release.
{{ config(tags=['report_only']) }}
-- Every open flag of this build, one row per flag (rule x product): the admin's Queue and Case pages read it, from
-- the ops tables written beside each release (releases/.../ops/mart_flags.parquet); never served or exported.
-- Each row carries its product's summary from int_catalog_decided (ready_flags, evidence_hash) and
-- public_rows: the public rows a removal touches, its public_coffee row and its public_price_event rows (this
-- build's, else the served release's for a product this build already removes). And what the Case page shows of
-- the product, so it reads one file: its classifier answer, link, tags, bag sizes, prices and description.
{%- set prev = served_file('public_price_event') %}
with flags as ({{ all_flags() }}),
events as (
    select coffee_id, count(*) as n from {{ ref('public_price_event') }} group by coffee_id
),
prev_events as (
    {%- if prev %}
    select coffee_id, count(*) as n from read_parquet('{{ prev }}', hive_partitioning = false) group by coffee_id
    {%- else %}
    select null::varchar as coffee_id, 0 as n where false
    {%- endif %}
)
select
    f.rule, f.family, f.polarity, f.direction, f.coffee_id, f.roaster_id, c.input_hash, c.title, c.product_type,
    f.evidence, f.evidence_key, d.evidence_hash, d.decision, d.classifier_is_coffee, d.was_public,
    d.ready_flags, d.coverage_problem, d.supports_families, d.doubts, d.restores,
    -- this build's public row and events, else the served release's; a product public in neither touches none
    case when f.coffee_id in (select coffee_id from {{ ref('public_coffee') }}) then 1 + coalesce(e.n, 0)
         when d.was_public then 1 + coalesce(pe.n, 0) else 0 end as public_rows,
    c.verdict_label, c.classified_by, c.reason, c.url, c.tags,
    list_transform(c.variants, lambda v: v.grams) as bag_sizes_g,
    list_transform(c.variants, lambda v: v.price_usd) as prices_usd,
    left(i.body_text, 2000) as description
from flags f
join {{ ref('int_catalog_decided') }} d using (coffee_id)
join {{ ref('int_catalog_candidates') }} c using (coffee_id)
left join {{ ref('stg_product_inputs') }} i using (input_hash)
left join events e using (coffee_id)
left join prev_events pe using (coffee_id)
