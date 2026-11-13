-- Warn (review of #137, N1): a store whose canonical id is unreachable while one of its aliases still answers. The
-- alias rule never reads the crawl status (macros/roaster_alias.sql), so an outage never splits a store; but a
-- canonical domain that has died for good (no ok probe, so its host never moves) keeps the store, which nobody
-- crawls any more: the canonical is unreachable and the alias is never crawled. A row here is that store. If the
-- canonical's domain is gone for good, set its alias_of in config/roasters.csv to the alias that still answers
-- (RUNBOOK, release changes), so the store is crawled under that id again. Warn only: a few weeks down is an outage.
{{ config(severity='warn', store_failures=true) }}
with last_probe as (
    select roaster_id, status, probed_at from {{ ref('stg_probes') }}
    where status <> 'robots_unavailable'
    qualify row_number() over (partition by roaster_id order by probed_at desc, status) = 1
)
select a.roaster_id as alias_id, c.roaster_id as canonical_id, c.last_fetch as canonical_last_fetch,
       p.probed_at as alias_last_ok_probe
from {{ ref('int_roaster_state') }} a
join {{ ref('int_roaster_state') }} c on c.roaster_id = a.canonical_roaster_id
join last_probe p on p.roaster_id = a.roaster_id
where a.roaster_id <> a.canonical_roaster_id
  and c.crawl_status = 'unreachable'
  and p.status = 'ok'
