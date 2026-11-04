{{ config(severity='warn') }}
-- For the fetch-health check only (attempted_only): an edge's challenge is systemic for a platform when that one edge
-- alone failed more of the platform's roasters than run_ok_rate lets the whole run fail (1 - min_ok_share), on a
-- platform big enough for adapter_ok_rate to judge (min_adapter_roasters roasters with a catalog, reached). Both are
-- the gate's own numbers, reused: under 20 roasters one store moves the share 5+ points, and a share under the run's
-- tolerance is what a normal week already absorbs. So one challenged store never makes it. Counted over
-- run_completeness's has_catalog and attempted roasters, as adapter_ok_rate; only an edge a recorded block names
-- (stg_fetches.challenge: a bare 202 names none). The weekly build never sets attempted_only: nothing to judge there.
{% if var('as_of_run') and var('attempted_only') %}
with judged as (
    select adapter, challenge from {{ ref('run_completeness') }} where has_catalog and attempted
),
platforms as (
    select adapter, count(*) as roasters from judged group by adapter
    having count(*) >= {{ var('min_adapter_roasters') }}
)
select j.challenge as edge, j.adapter, p.roasters, count(*) as challenged,
       count(*)::double / p.roasters as challenged_share
from judged j join platforms p using (adapter)
where j.challenge is not null
group by j.challenge, j.adapter, p.roasters
having count(*)::double / p.roasters > 1 - {{ var('min_ok_share') }}
{% else %}
select 1 as skipped where false
{% endif %}
