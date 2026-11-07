-- One row per product (coffee_id), as of its latest record: what the rules (flag_*) read. The classifier's own
-- answer (is_coffee, before the owner's decision and the guard), the owner's decision, and the served release's
-- view of it: was_public (in the served public_coffee; var previous_release, false when nothing is served).
-- removed_since: when the classifier stopped calling it coffee: the earliest record after its last coffee record
-- (or, never coffee now, any record), each dated by the later of its run and its verdict (a verdict arriving for an
-- unchanged version removes it too). Later edits to the product don't move it. Null while it is coffee.
-- relevant: a product the flags are about, public in the served release or coffee now; the rest are already out.
{%- set prev = served_file('public_coffee') %}
with prev as (
    {%- if prev %}
    select coffee_id from read_parquet('{{ prev }}', hive_partitioning = false)
    {%- else %}
    select null::varchar as coffee_id where false
    {%- endif %}
),
records as (
    select s.coffee_id, s.roaster_id, s.product_id, s.run_date, s.input_hash, s.title, s.product_type, s.tags,
           s.body_hash, s.variants, s.url, s.decision, p.is_coffee,
           greatest(s.run_date, cast(v.decided_at as date)) as answered_on,
           max(case when p.is_coffee then s.run_date end) over (partition by s.coffee_id) as last_coffee_run,
           min(s.run_date) over (partition by s.coffee_id) as first_record
    from {{ ref('stg_snapshots') }} s
    join {{ ref('stg_products') }} p using (input_hash)
    left join {{ ref('stg_verdicts') }} v using (input_hash)
),
removed as (
    select coffee_id, min(answered_on) as removed_since from records
    where not is_coffee and (last_coffee_run is null or run_date > last_coffee_run)
    group by coffee_id
),
latest as (
    select * from records
    qualify row_number() over (partition by coffee_id order by run_date desc, input_hash) = 1
)
select
    l.coffee_id, l.roaster_id, l.product_id, l.run_date, l.input_hash, l.title, l.product_type, l.tags, l.body_hash,
    l.variants, l.url, l.decision, p.is_coffee, p.classified_by, p.reason, v.label as verdict_label, l.first_record,
    case when not p.is_coffee then r.removed_since end as removed_since,
    l.coffee_id in (select coffee_id from prev) as was_public,
    l.coffee_id in (select coffee_id from prev) or p.is_coffee or l.decision = 'include' as relevant
from latest l
join {{ ref('stg_products') }} p using (input_hash)
left join {{ ref('stg_verdicts') }} v using (input_hash)
left join removed r using (coffee_id)
