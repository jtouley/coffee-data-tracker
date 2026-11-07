-- The guard (docs/design/admin-registry.md §2.4, M2; settings guard in var rules): an error when it holds more than
-- guard.max_held products (too many doubted removals at once is a classifier or data problem to look at, not one
-- to hold silently), and on any held product the guard's own rule doesn't allow: mode off, no doubt from a
-- guard.doubted_by rule, not a classifier removal, against the owner's decision, or past guard.max_weeks.
{%- set g = var('rules')['guard'] %}
with held as (select * from {{ ref('int_catalog_decided') }} where held),
n as (select count(*) as held from held)
select 'over max_held' as problem, cast(held as varchar) as detail from n where held > {{ g['max_held'] }}
union all
select 'held against its rule', coffee_id from held
where {{ 'true' if g['mode'] != 'remove_unless_doubted' else 'false' }}
   or decision is not null or classifier_is_coffee or not was_public or doubts = 0
