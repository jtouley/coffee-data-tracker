-- Every query, place, filter combination, source, campaign tag and click context a mart shows met its threshold
-- (design section 3): usage_min_days distinct days in the window ending with the week (usage_min_days_unknown_word
-- for a query with a word outside the vocabulary). Recomputed here from int_usage_events, independently of the marts'
-- own SQL. Returns one row per shown value that didn't.
{{ config(severity='error') }}
{%- set w = var('usage_window_days') %}
with ev as (
    select * from {{ ref('int_usage_events') }} where not bot
),

query_days as (
    select m.week, m.query as value, 'search_terms' as report,
        count(distinct e.received_date) as days,
        if(bool_and(e.query_words_known), {{ var('usage_min_days') }}, {{ var('usage_min_days_unknown_word') }}) as need
    from {{ ref('mart_usage_search_terms_weekly') }} as m
    left join ev as e
        on e.event = 'search' and e.query_norm = m.query
        and e.received_date between m.week + 6 - ({{ w }} - 1) and m.week + 6
    where m.query <> '(rare)'
    group by all
),

place_days as (
    select m.week, m.place_type || ':' || m.place as value, 'places' as report,
        count(distinct e.received_date) as days, {{ var('usage_min_days') }} as need
    from {{ ref('mart_usage_places_weekly') }} as m
    left join ev as e
        on e.event = 'search'
        and list_contains(list_transform(if(m.place_type = 'city', e.cities, e.states), lambda x: lower(trim(x))), m.place)
        and e.received_date between m.week + 6 - ({{ w }} - 1) and m.week + 6
    where m.place <> '(rare)'
    group by all
),

combo_days as (
    select m.week, concat_ws('|', m.origin, m.process, m.state, m.in_stock, m.sort) as value, 'filter_combos' as report,
        count(distinct e.received_date) as days, {{ var('usage_min_days') }} as need
    from {{ ref('mart_usage_filter_combos_weekly') }} as m
    left join ev as e
        on e.event in ('search', 'filter_state')
        and coalesce(e.origin, 'any') = m.origin and coalesce(e.process, 'any') = m.process
        and coalesce(e.state, 'any') = m.state and if(e.in_stock, 'yes', 'any') = m.in_stock
        and coalesce(e.sort, 'default') = m.sort
        and e.received_date between m.week + 6 - ({{ w }} - 1) and m.week + 6
    where m.origin <> '(rare)'
    group by all
),

source_days as (
    select m.week, concat_ws('|', m.source_kind, m.source, m.utm_medium, m.utm_campaign) as value, 'sources' as report,
        count(distinct e.received_date) as days, {{ var('usage_min_days') }} as need
    from {{ ref('mart_usage_sources_weekly') }} as m
    left join ev as e
        on e.source_kind = m.source_kind and e.source = m.source
        and (m.utm_medium = '(rare)' or coalesce(e.utm_medium, 'none') = m.utm_medium)
        and (m.utm_campaign = '(rare)' or coalesce(e.utm_campaign, 'none') = m.utm_campaign)
        and e.received_date between m.week + 6 - ({{ w }} - 1) and m.week + 6
    where m.source <> '(rare)'
    group by all
),

context_days as (
    select m.week, m.roaster_id || '|' || m.context_kind || '|' || m.context_value as value, 'click_context' as report,
        count(distinct e.received_date) as days,
        if(m.context_kind = 'query' and not bool_and(e.query_words_known),
           {{ var('usage_min_days_unknown_word') }}, {{ var('usage_min_days') }}) as need
    from {{ ref('mart_click_context_weekly') }} as m
    left join ev as e
        on e.event = 'click_out' and e.roaster_id = m.roaster_id
        and e.received_date between m.week + 6 - ({{ w }} - 1) and m.week + 6
        and case m.context_kind
            when 'query' then e.query_norm = m.context_value
            when 'searched_city' then list_contains(list_transform(e.cities, lambda x: lower(trim(x))), m.context_value)
            when 'searched_state' then list_contains(list_transform(e.states, lambda x: lower(trim(x))), m.context_value)
            else concat('origin=', coalesce(e.origin, 'any'), '; process=', coalesce(e.process, 'any'), '; state=',
                        coalesce(e.state, 'any'), '; in_stock=', if(e.in_stock, 'yes', 'any'), '; sort=',
                        coalesce(e.sort, 'default')) = m.context_value
        end
    where m.context_value <> '(rare)'
    group by m.week, m.roaster_id, m.context_kind, m.context_value
),

source_labels as (
    -- the source a roaster's clicks or a city's facet cell is shown with: the same days as the sources mart
    select m.week, m.source as value, 'roaster_clicks.source' as report
    from {{ ref('mart_roaster_clicks_weekly') }} as m
    where m.source not in ('(rare)', 'not recorded')
    union
    select m.week, m.facet_value, 'city_facets.source'
    from {{ ref('mart_usage_city_facets_weekly') }} as m
    where m.facet = 'source' and m.facet_value not in ('(rare)', '(other)')
),

label_days as (
    select l.week, l.value, l.report,
        count(distinct e.received_date) as days, {{ var('usage_min_days') }} as need
    from source_labels as l
    left join ev as e
        on e.source = l.value
        and e.received_date between l.week + 6 - ({{ w }} - 1) and l.week + 6
    group by all
),

checked as (
    select * from query_days
    union all select * from place_days
    union all select * from combo_days
    union all select * from source_days
    union all select * from context_days
    union all select * from label_days
)

select report, week, days, need from checked where days < need
