-- Fails the build: a coverage claim (marketplace/LISTING.md) naming a metric mart_build_profile doesn't have is a
-- bug in the claim or the profile, not data drift, and coverage_claims_match_build would silently skip it.
{{ config(store_failures=true) }}
with claims as ({{ coverage_claims() }})
select source, metric, text
from claims
where metric not in (select metric from {{ ref('mart_build_profile') }})
