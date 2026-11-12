-- One row per importer lot ever stored (int_importer_lots' lot_key), available or not: when it was first and last
-- seen, and flags for whether it is offered. Rebuilt from every stored week at each build (raw is append-only, so
-- this is the lot's whole history, never a stateful snapshot); a lot's week-by-week values stay in importer_offers.
--   is_available: whether the lot was available in the last week it was seen.
--   currently_offered: seen in its importer's latest stored week and available then.
with lots as (
    select * from {{ ref('int_importer_lots') }}
),

latest as (
    select importer, max(week) as latest_week from lots group by importer
),

history as (
    select
        importer,
        lot_key,
        min(week) as first_seen_week,
        max(week) as last_seen_week,
        count(distinct week) as weeks_seen,
        arg_max(is_available, week) as is_available
    from lots
    group by importer, lot_key
)

select
    h.importer,
    h.lot_key,
    h.first_seen_week,
    h.last_seen_week,
    h.weeks_seen,
    h.is_available,
    h.last_seen_week = l.latest_week and coalesce(h.is_available, false) as currently_offered
from history as h
inner join latest as l using (importer)
