{#- Raw events cast to the row's types and deduplicated by event_id (the collector's retry id): one row per event.
    Both staging models over raw use it, so they keep the same row for an event. The pick has a total order: the
    earliest received_at, then the row's content (md5 of its JSON; received_at is already a plain UTC timestamp, so
    the text doesn't depend on the session's zone), then the file's name, which is the only thing two copies of one
    retry can differ in. -#}
{% macro usage_events_deduped() -%}
with typed as (
    select
        cast(event_id as varchar) as event_id,
        -- a zone-aware timestamp is read as UTC; a plain one is UTC already (the collector writes server time)
        cast(received_at as timestamp) as received_at,
        cast(event as varchar) as event, cast(surface as varchar) as surface, cast(release as varchar) as release,
        cast(device as varchar) as device, cast(query as varchar) as query,
        cast(cities as varchar[]) as cities, cast(states as varchar[]) as states,
        cast(origin as varchar) as origin, cast(process as varchar) as process, cast(state as varchar) as state,
        cast(in_stock as boolean) as in_stock, cast(sort as varchar) as sort,
        cast(results_n as integer) as results_n, cast(zero_results as boolean) as zero_results,
        cast(roaster_id as varchar) as roaster_id, cast(coffee_id as varchar) as coffee_id,
        cast(rank as integer) as rank, cast(source as varchar) as source,
        cast(referrer_domain as varchar) as referrer_domain,
        cast(utm_source as varchar) as utm_source, cast(utm_medium as varchar) as utm_medium,
        cast(utm_campaign as varchar) as utm_campaign, cast(ip_prefix as varchar) as ip_prefix,
        cast(bot as boolean) as bot,
        parse_filename(filename) as file_name
    from {{ usage_raw() }} as r
    where event_id is not null
),

ranked as (
    select *,
        row_number() over (
            partition by event_id
            order by received_at, md5(to_json(struct_pack(event, surface, release, device, query, cities, states,
                origin, process, state, in_stock, sort, results_n, zero_results, roaster_id, coffee_id, rank,
                source, referrer_domain, utm_source, utm_medium, utm_campaign, ip_prefix, bot))), file_name
        ) as copy_n
    from typed
)

select * exclude (copy_n, file_name) from ranked where copy_n = 1
{%- endmacro %}
