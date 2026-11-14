-- One row per product the rules flagged (coffee_id): its flags summed up by polarity, and whether its removal is
-- ready as far as the build can tell (docs/design/admin-registry.md §2.4; settings in var rules, from
-- coffeedata.rules). Nothing here changes what is public: a removal is the owner's decision, published by a draft.
--   ready_flags: supports from readiness.supports_min distinct families (or flags, when distinct_families is
--     false), no doubts, and no coverage problem (the store's latest run failed to fetch it, or fetched part of
--     it), when readiness.blocked_by names coverage. The live check is the admin's, never the build's.
--   evidence_hash: the store and the flags' evidence without the product's identity: a store's products with the
--     same evidence group.
{%- set r = var('rules')['readiness'] %}
with flags as ({{ all_flags() }}),
per as (
    select coffee_id, any_value(roaster_id) as roaster_id,
           count(distinct family) filter (where polarity = 'supports') as supports_families,
           count(*) filter (where polarity = 'supports') as supports,
           count(*) filter (where polarity = 'doubts') as doubts,
           count(*) filter (where polarity = 'restore') as restores,
           -- the store is part of it: a group is one store's products with the same evidence, so one live check
           -- (a test crawl of the store) speaks for the whole group, and never for another store
           md5(any_value(roaster_id) || '|' || string_agg(rule || ':' || direction || ':' || coalesce(evidence_key, ''),
                                                          '|' order by rule, direction, evidence_key)) as evidence_hash
    from flags group by coffee_id
),
-- A coverage problem: the store's latest run failed to fetch it, or read only part of its catalog.
coverage as (
    select roaster_id from {{ ref('int_roaster_runs') }}
    qualify row_number() over (partition by roaster_id order by run_date desc) = 1 and (not ok or partial)
)
select
    p.coffee_id, p.roaster_id, c.decision, c.is_coffee as classifier_is_coffee, c.was_public,
    p.supports_families, p.supports, p.doubts, p.restores, p.evidence_hash,
    p.roaster_id in (select roaster_id from coverage) as coverage_problem,
    {{ 'p.supports_families' if r['distinct_families'] else 'p.supports' }} >= {{ r['supports_min'] }}
        and p.doubts = 0
        {%- if 'coverage' in r['blocked_by'] %}
        and p.roaster_id not in (select roaster_id from coverage)
        {%- endif %} as ready_flags
from per p
join {{ ref('int_catalog_candidates') }} c using (coffee_id)
