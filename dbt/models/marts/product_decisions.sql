-- Report only: `coffeedata build` runs it after the gate (tag report_only), so it can never hold back a release.
{{ config(tags=['report_only']) }}
-- Every product a store lists, coffee or not, as of its latest record: whether the build counts it as coffee, what
-- decided (Claude's verdict or the rules), and why (stg_products.reason: verdict:<label>, or the rule that decided;
-- decision:include or decision:exclude when the owner's decision overrides them; held:guard while the guard keeps it).
-- dim_coffee keeps only the coffees; this is the owner's view of the rest, written beside each release as an ops
-- table (quality.OPS_TABLES, releases/.../build=<id>/ops/) and never served, exported or published.
with s as (
    select s.coffee_id, s.roaster_id, s.product_id, s.title, s.product_type, s.url, s.run_date, s.input_hash, s.decision,
           min(s.run_date) over (partition by s.coffee_id) as first_seen
    from {{ ref('stg_snapshots') }} s
    qualify row_number() over (partition by s.coffee_id order by s.run_date desc, s.input_hash) = 1
)
select
    s.coffee_id, s.roaster_id, s.product_id, s.title, s.product_type, s.url, s.first_seen,
    s.run_date as last_seen, run.is_current as is_listed, s.input_hash,
    {{ decided('s.decision', 'p.is_coffee', 's.coffee_id') }} as is_coffee, p.classified_by, p.verdict_model, v.label as verdict_label,
    case when s.decision is not null then 'decision:' || s.decision
         when s.coffee_id in (select coffee_id from {{ ref('int_catalog_decided') }} where held) then 'held:guard'
         else p.reason end as reason
from s
join {{ ref('stg_products') }} p using (input_hash)
join {{ ref('stg_runs') }} run using (roaster_id, run_date)
left join {{ ref('stg_verdicts') }} v using (input_hash)
