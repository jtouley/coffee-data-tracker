-- Warn only (eval/dq/README.md, #12): a duplicate roaster is a registry fix (merge or drop one id), not a reason to
-- hold back this week's release, which already served the same duplicates last week.
{{ config(severity='warn', store_failures=true) }}
-- One roaster's catalog listed under two roaster_ids (an alias domain, a rebrand, a *.myshopify.com host beside the
-- custom domain), so its coffees are counted twice. Two roasters with listed, non-wholesale coffees are a pair when
-- they share at least near_duplicate_min_shared titles (lower case, anything but a-z0-9 as one space) and
--   * the shared titles are at least near_duplicate_min_containment of the smaller list, and
--   * the shared titles listed by at most near_duplicate_rare_roasters roasters are at least
--     near_duplicate_min_rare_containment of it, so that a few generic titles ("Colombia", "Decaf",
--     "House Blend") never make a pair.
-- One row per pair. On the 2026-11-02 release this is 37 pairs; every one labelled was a real duplicate.
with t as (
    select distinct roaster_id, trim(regexp_replace(lower(title), '[^a-z0-9]+', ' ', 'g')) as title
    from {{ ref('int_current_catalog') }}
    where not is_wholesale and title is not null
),
titles as (select * from t where title <> ''),
listed_by as (select title, count(*) as roasters from titles group by title),
sizes as (select roaster_id, count(*) as titles from titles group by roaster_id),
pairs as (
    select a.roaster_id as roaster_a, b.roaster_id as roaster_b, count(*) as shared,
           count(*) filter (where l.roasters <= {{ var('near_duplicate_rare_roasters') }}) as shared_rare
    from titles a
    join titles b on a.title = b.title and a.roaster_id < b.roaster_id
    join listed_by l on l.title = a.title
    group by all
)
select p.roaster_a, p.roaster_b, p.shared, p.shared_rare, sa.titles as titles_a, sb.titles as titles_b
from pairs p
join sizes sa on sa.roaster_id = p.roaster_a
join sizes sb on sb.roaster_id = p.roaster_b
where p.shared >= {{ var('near_duplicate_min_shared') }}
  and p.shared >= {{ var('near_duplicate_min_containment') }} * least(sa.titles, sb.titles)
  and p.shared_rare >= {{ var('near_duplicate_min_rare_containment') }} * least(sa.titles, sb.titles)
