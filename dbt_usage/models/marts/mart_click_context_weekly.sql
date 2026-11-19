-- One row per (week, roaster_id, context_kind, context_value): what led to a roaster's click-outs, from the search
-- and filter state the click carried. context_kind is query, searched_city, searched_state (places named in the
-- search text) or filters (the combination, as text). A value is shown only once it led to that roaster's clicks on
-- usage_min_days distinct days in the window (usage_min_days_unknown_word for a query with a word outside the
-- vocabulary); the rest count as '(rare)'. Never the visitor's place (design section 5). Bots left out.
with clicks as (
    select *
    from {{ ref('int_usage_events') }}
    where event = 'click_out' and roaster_id is not null and not bot
),

contexts as (
    select week, received_date, roaster_id, 'query' as context_kind, query_norm as context_value,
        query_words_known as known
    from clicks where query_norm is not null
    union all
    select week, received_date, roaster_id, 'searched_city', lower(trim(unnest(cities))), true from clicks
    union all
    select week, received_date, roaster_id, 'searched_state', lower(trim(unnest(states))), true from clicks
    union all
    select week, received_date, roaster_id, 'filters',
        concat('origin=', coalesce(origin, 'any'), '; process=', coalesce(process, 'any'), '; state=',
               coalesce(state, 'any'), '; in_stock=', if(in_stock, 'yes', 'any'), '; sort=',
               coalesce(sort, 'default')),
        true
    from clicks
),

named as (
    select * from contexts where context_value is not null and context_value <> ''
),

days as {{ days_in_window('named', ['roaster_id', 'context_kind', 'context_value']) }}

select n.week, n.roaster_id, n.context_kind,
    case
        when d.days >= if(n.known, {{ var('usage_min_days') }}, {{ var('usage_min_days_unknown_word') }})
            then n.context_value
        else '(rare)'
    end as context_value,
    count(*) as click_outs
from named as n
inner join days as d
    on d.week = n.week and d.roaster_id = n.roaster_id and d.context_kind = n.context_kind
    and d.context_value = n.context_value
group by all
