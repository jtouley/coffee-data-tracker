-- Warn-only: the coverage we publish (marketplace/LISTING.md, typed by hand into the Snowflake listing) should
-- match what this build serves. A claim more than claim_tolerance from its mart_build_profile metric is listed,
-- with the line to update; `coffeedata build` prints them. Real weekly change is drift to fix in the copy, not a
-- reason to hold a release back.
{{ config(severity='warn', store_failures=true) }}
with claims as ({{ coverage_claims() }})
select c.source, c.metric, c.text, c.claimed, p.value as built,
       (c.claimed - p.value) / nullif(p.value, 0) as off_by, {{ var('claim_tolerance') }} as tolerance
from claims c
join {{ ref('mart_build_profile') }} p using (metric)
where abs(c.claimed - p.value) > {{ var('claim_tolerance') }} * abs(p.value)
