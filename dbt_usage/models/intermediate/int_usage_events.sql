-- One row per event, with what the reports group by: the normalized query and whether every word of it is in the
-- release's vocabulary, the traffic source, and the location int_usage_location found. Bot events stay, flagged
-- (bot); the marts leave them out.
with events as (
    select *,
        nullif(regexp_replace(lower(strip_accents(trim(query))), '\s+', ' ', 'g'), '') as query_norm
    from {{ ref('stg_usage_events') }}
),

query_words as (
    select event_id, unnest(regexp_split_to_array(query_norm, '[^\p{L}\p{N}]+')) as word
    from events
    where query_norm is not null
),

known as (
    select q.event_id, bool_and(v.word is not null) as query_words_known
    from query_words as q
    left join {{ ref('int_usage_vocabulary') }} as v on v.word = q.word
    where q.word <> ''
    group by q.event_id
)

select
    e.event_id, e.received_at, e.received_date, e.week, e.event, e.surface, e.release, e.device,
    e.query_norm,
    coalesce(k.query_words_known, false) as query_words_known,
    e.cities, e.states, e.origin, e.process, e.state, e.in_stock, e.sort, e.results_n, e.zero_results,
    e.roaster_id, e.coffee_id, e.rank,
    -- the traffic source the collector parsed on a load's first event (utm, referrer, direct or internal; design
    -- section 5); null on the load's later events
    e.source as source_kind,
    case
        when e.source = 'utm' then lower(e.utm_source)
        when e.source = 'referrer' then coalesce(lower(e.referrer_domain), '(unnamed)')
        else e.source
    end as source,
    lower(e.utm_medium) as utm_medium, lower(e.utm_campaign) as utm_campaign,
    l.country, l.region, l.city, l.city_population,
    e.bot
from events as e
left join known as k on k.event_id = e.event_id
left join {{ ref('int_usage_location') }} as l on l.event_id = e.event_id
