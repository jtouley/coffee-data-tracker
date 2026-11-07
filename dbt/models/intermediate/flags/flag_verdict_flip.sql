-- Rule verdict_flip (family classifier; coffeedata.rules): the classifier's answer differs from the served
-- release's. removed: public there, not coffee now. added: seen by the served release's run but not public there,
-- coffee now. A product the owner decided is the owner's, not the classifier's: no flag. Nothing served: no flags.
-- One row per flag (the flag_* contract): coffee_id, roaster_id, direction, evidence, evidence_key.
{%- set prev = served_file('public_coffee') %}
with prev_run as (
    select coalesce(cast(nullif('{{ var("previous_release_run_date", "") }}', '') as date),
        {%- if prev %} (select max(last_seen) from read_parquet('{{ prev }}', hive_partitioning = false))
        {%- else %} null{% endif %}) as run_date
),
f as (
    select c.*, case when c.was_public and not c.is_coffee then 'removed'
                     when not c.was_public and c.is_coffee and c.first_record <= r.run_date then 'added' end as direction
    from {{ ref('int_catalog_candidates') }} c, prev_run r
    where c.decision is null and r.run_date is not null and {{ 'true' if prev else 'false' }}  -- nothing served
)
select coffee_id, roaster_id, direction,
       to_json({'verdict': verdict_label, 'classified_by': classified_by, 'reason': reason,
                'since': version_since}) as evidence,
       direction || ':' || coalesce('verdict:' || verdict_label, reason) as evidence_key
from f where direction is not null
