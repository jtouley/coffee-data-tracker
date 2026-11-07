-- One row per product the rules flagged (coffee_id): its flags summed up by polarity, whether the guard holds it,
-- and whether its removal is ready as far as the build can tell (docs/design/admin-registry.md §2.4; settings in
-- var rules, from coffeedata.rules).
--   held: the guard (mode remove_unless_doubted) keeps a product the classifier removes from the served release
--     (verdict_flip removed) while a guard.doubted_by rule doubts it, for at most guard.max_weeks after the
--     classifier's answer changed, and never against the owner's decision. Macro decided() counts a held product as
--     coffee. Mode off (the default): nothing is held, and the build is the classifier's alone.
--   ready_flags: supports from readiness.supports_min distinct families (or flags, when distinct_families is
--     false), no doubts, and no coverage problem (the store's latest run failed to fetch it, or fetched part of it), when
--     readiness.blocked_by names coverage. The live check is the admin's, never the build's.
--   evidence_hash: the flags' evidence without the product's identity, so products with the same evidence group.
{%- set g = var('rules')['guard'] %}
{%- set r = var('rules')['readiness'] %}
with flags as ({{ all_flags() }}),
per as (
    select coffee_id, any_value(roaster_id) as roaster_id,
           count(distinct family) filter (where polarity = 'supports') as supports_families,
           count(*) filter (where polarity = 'supports') as supports,
           count(*) filter (where polarity = 'doubts') as doubts,
           count(*) filter (where polarity = 'restore') as restores,
           bool_or(polarity = 'doubts' and rule in ({{ "'" ~ g['doubted_by'] | join("', '") ~ "'" }})) as guard_doubt,
           bool_or(rule = 'verdict_flip' and direction = 'removed') as removed_by_classifier,
           md5(string_agg(rule || ':' || direction || ':' || coalesce(evidence_key, ''), '|'
                          order by rule, direction, evidence_key)) as evidence_hash
    from flags group by coffee_id
),
-- A coverage problem: the store's latest run failed to fetch it, or read only part of its catalog.
coverage as (
    select roaster_id from {{ ref('int_roaster_runs') }}
    qualify row_number() over (partition by roaster_id order by run_date desc) = 1 and (not ok or partial)
),
as_of as (select max(run_date) as run_date from {{ ref('int_catalog_candidates') }})
select
    p.coffee_id, p.roaster_id, c.decision, c.is_coffee as classifier_is_coffee, c.was_public, c.version_since,
    p.supports_families, p.supports, p.doubts, p.restores, p.evidence_hash,
    p.roaster_id in (select roaster_id from coverage) as coverage_problem,
    {{ 'true' if g['mode'] == 'remove_unless_doubted' else 'false' }}
        and p.removed_by_classifier and coalesce(p.guard_doubt, false) and c.decision is null
        and date_diff('day', c.version_since, a.run_date) <= 7 * {{ g['max_weeks'] }} as held,
    {{ 'p.supports_families' if r['distinct_families'] else 'p.supports' }} >= {{ r['supports_min'] }}
        and p.doubts = 0
        {%- if 'coverage' in r['blocked_by'] %}
        and p.roaster_id not in (select roaster_id from coverage)
        {%- endif %} as ready_flags
from per p
join {{ ref('int_catalog_candidates') }} c using (coffee_id)
cross join as_of a
