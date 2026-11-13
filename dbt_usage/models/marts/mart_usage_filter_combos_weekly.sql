-- One row per (week, origin, process, state, in_stock, sort): the filter combinations results were shown with
-- (search and filter_state events), 'any' for a filter left unset. A combination is shown only once it occurred on
-- usage_min_days distinct days in the window; the rest count under '(rare)' in every column. Bots left out.
with shown as (
    select week, received_date,
        coalesce(origin, 'any') as origin, coalesce(process, 'any') as process, coalesce(state, 'any') as state,
        if(in_stock, 'yes', 'any') as in_stock, coalesce(sort, 'default') as sort, zero_results
    from {{ ref('int_usage_events') }}
    where event in ('search', 'filter_state') and not bot
),

days as {{ days_in_window('shown', ['origin', 'process', 'state', 'in_stock', 'sort']) }},

judged as (
    select s.*, d.days >= {{ var('usage_min_days') }} as passes
    from shown as s
    inner join days as d
        on d.week = s.week and d.origin = s.origin and d.process = s.process and d.state = s.state
        and d.in_stock = s.in_stock and d.sort = s.sort
)

select week,
    if(passes, origin, '(rare)') as origin, if(passes, process, '(rare)') as process,
    if(passes, state, '(rare)') as state, if(passes, in_stock, '(rare)') as in_stock,
    if(passes, sort, '(rare)') as sort,
    count(*) as result_views,
    count(*) filter (where zero_results) as zero_result_views
from judged
group by all
