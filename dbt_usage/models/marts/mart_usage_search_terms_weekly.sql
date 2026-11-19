-- One row per (week, query): what people searched for, normalized (lowercase, no accents, single spaces). A query
-- is shown only once it was searched on usage_min_days distinct days in the window ending with the week, or
-- usage_min_days_unknown_word when a word of it isn't in the release's vocabulary; the rest count as '(rare)'
-- (design section 3). Bots left out. Never with a city or anything finer than the week.
with searches as (
    select week, received_date, query_norm, query_words_known, zero_results
    from {{ ref('int_usage_events') }}
    where event = 'search' and not bot and query_norm is not null
),

days as {{ days_in_window('searches', ['query_norm']) }}

select
    s.week,
    case
        when d.days >= if(s.query_words_known, {{ var('usage_min_days') }}, {{ var('usage_min_days_unknown_word') }})
            then s.query_norm
        else '(rare)'
    end as query,
    count(*) as searches,
    count(*) filter (where s.zero_results) as zero_result_searches
from searches as s
inner join days as d on d.week = s.week and d.query_norm = s.query_norm
group by all
