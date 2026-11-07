-- One row per product (coffee_id), as of its latest record: what the rules (flag_*) read. The classifier's own
-- answer (is_coffee, before the owner's decision and the guard), the owner's decision, and the served release's
-- view of it: was_public (in the served public_coffee; var previous_release, false when nothing is served).
-- version_since: when the classifier's current answer appeared, the later of the latest version's first record and
-- its verdict (a verdict arriving for an unchanged version flips it too). relevant: a product the flags are about,
-- public in the served release or coffee now; the rest are already out and stay out.
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
           s.body_hash, s.variants, s.url, s.decision,
           min(s.run_date) over (partition by s.coffee_id) as first_record,
           min(s.run_date) over (partition by s.coffee_id, s.input_hash) as version_first
    from {{ ref('stg_snapshots') }} s
),
latest as (
    select * from records
    qualify row_number() over (partition by coffee_id order by run_date desc, input_hash) = 1
)
select
    l.coffee_id, l.roaster_id, l.product_id, l.run_date, l.input_hash, l.title, l.product_type, l.tags, l.body_hash,
    l.variants, l.url, l.decision, p.is_coffee, p.classified_by, p.reason, v.label as verdict_label, l.first_record,
    greatest(l.version_first, cast(v.decided_at as date)) as version_since,
    l.coffee_id in (select coffee_id from prev) as was_public,
    l.coffee_id in (select coffee_id from prev) or p.is_coffee or l.decision = 'include' as relevant
from latest l
join {{ ref('stg_products') }} p using (input_hash)
left join {{ ref('stg_verdicts') }} v using (input_hash)
