-- One row per event, as the collector stored it (raw, design section 0), deduplicated by event_id. The masked
-- address is left out: only stg_usage_ip_prefixes carries it, and only int_usage_location reads that (design
-- section 4).
-- A raw row with no event_id is dropped here; usage_rows_without_event_id (warn) counts them.
with events as ({{ usage_events_deduped() }})

select
    event_id, received_at,
    cast(received_at as date) as received_date,
    cast(date_trunc('week', received_at) as date) as week,
    event, surface, release, device, query, cities, states, origin, process, state, in_stock, sort, results_n,
    zero_results, roaster_id, coffee_id, rank, source, referrer_domain, utm_source, utm_medium, utm_campaign,
    coalesce(bot, false) as bot
from events
