-- One row per event: its IP prefix, as masked at collection (IPv4 /24, IPv6 /40). The only model besides raw that
-- holds the prefix, and only int_usage_location reads it (ip_prefix_only_in_location; design section 4). A view:
-- nothing is stored here, the prefix stays in raw alone.
with events as ({{ usage_events_deduped() }})

select event_id, ip_prefix from events
