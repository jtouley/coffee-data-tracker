-- A removal is never ready while a doubt is open (docs/design/admin-registry.md §2.4, M1: a doubt is never a vote to
-- remove), nor with fewer supports than readiness.supports_min. An error.
{%- set r = var('rules')['readiness'] %}
select coffee_id, doubts, supports, supports_families from {{ ref('int_catalog_decided') }}
where ready_flags and (doubts > 0 or {{ 'supports_families' if r['distinct_families'] else 'supports' }} < {{ r['supports_min'] }})
