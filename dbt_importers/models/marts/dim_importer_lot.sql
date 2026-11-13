-- One row per importer lot ever stored (int_importer_lots' lot_key; for Royal, one Shopify product, so a sample box
-- and a full bag of one Royal lot are two rows), available or not: when it was first and last
-- seen, and flags for whether it is offered. Rebuilt from every stored week at each build (raw is append-only, so
-- this is the lot's whole history, never a stateful snapshot); a lot's week-by-week values stay in importer_offers.
--   is_available: whether the lot was available in the last week it was seen; null when that week didn't say.
--   currently_offered: seen in the latest week stored for any importer, and available then. An importer whose fetch
--     stopped or failed that week has no lot currently offered: its last page is no longer current.
with lots as (
    select * from {{ ref('int_importer_lots') }}
),

latest as (
    select max(week) as latest_week from lots
),

history as (
    select
        importer,
        lot_key,
        min(week) as first_seen_week,
        max(week) as last_seen_week,
        count(distinct week) as weeks_seen,
        -- arg_max_null: a last week that doesn't say is null, never an earlier week's value
        arg_max_null(is_available, week) as is_available
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
cross join latest as l
