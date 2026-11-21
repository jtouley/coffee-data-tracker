-- Report only: `coffeedata build` runs it after the gate (tag report_only), so it can never hold back a release.
{{ config(tags=['report_only']) }}
-- Every product a store lists, coffee or not, as of its latest record: whether the build counts it as coffee, what
-- decided (Claude's verdict or the rules), and why (stg_products.reason: verdict:<label>, or the rule that decided).
-- dim_coffee keeps only the coffees; this is the owner's view of the rest, written beside each release as an ops
-- table (quality.OPS_TABLES, releases/.../build=<id>/ops/) and never served, exported or published.
with s as (
    select s.coffee_id, s.roaster_id, s.product_id, s.title, s.product_type, s.url, s.run_date, s.input_hash,
           min(s.run_date) over (partition by s.coffee_id) as first_seen
    from {{ ref('stg_snapshots') }} s
    qualify row_number() over (partition by s.coffee_id order by s.run_date desc, s.input_hash) = 1
)
select
    s.coffee_id, s.roaster_id, s.product_id, s.title, s.product_type, s.url, s.first_seen,
    s.run_date as last_seen, run.is_current as is_listed, s.input_hash,
    p.is_coffee, p.classified_by, p.verdict_model, v.label as verdict_label, p.reason
from s
join {{ ref('stg_products') }} p using (input_hash)
join {{ ref('stg_runs') }} run using (roaster_id, run_date)
left join {{ ref('stg_verdicts') }} v using (input_hash)
