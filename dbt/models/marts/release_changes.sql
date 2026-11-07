-- Report only: `coffeedata build` runs it after the gate (tag report_only), so it can never hold back a release.
{{ config(tags=['report_only']) }}
-- What this build's public tables change against the release customers get now (var previous_release: local copies
-- of the served release's files; empty when nothing was served): one row per added, removed or changed row, with
-- the reason the build can show for it. release_changes_explained warns on every 'unexplained' row and on every
-- comparison that couldn't be made in full; `coffeedata build` prints the counts per reason and records them in the
-- build's record (kind release_change) as the release's notes.
--   public_coffee (coffee_id): removed because its roaster left the public tables (roaster_left), or because its
--     product's latest record is no longer classified as coffee (reclassified); added because it was first seen
--     after the previous release's run (new), its roaster joined (roaster_joined), or it was seen by then and is
--     now classified as coffee (reclassified). Changed in place (comparing the business columns only: weeks_listed
--     and last_seen move every week by design): delisted, relisted, history_changed or attributes_changed.
--   history_changed: first_seen moved, or weeks_listed moved by other than the runs since the previous release in
--     which the coffee was seen (its weekly advance). That is an earlier version's classification flipping.
--   public_price_event (every column): removed with its coffee (coffee_left), added with it (coffee_joined), from a
--     run after the previous release's (new_run), or on a coffee whose history changed (history_changed: its events
--     are re-derived from the versions now counted as coffee). Any other added or removed event (a past price
--     rewritten on a coffee whose history didn't change) is unexplained.
--   public_roaster (roaster_id): removed when no coffee of it is left in dim_coffee (no_coffee_left), when it opted
--     out (opted_out), when it's no longer known from a public source (source), or when it's outside the US
--     (outside_us: public_roaster keeps US stores only); added with its first public
--     coffee (first_coffee). Changed in place: values_changed (last_ok_run, which moves every week, isn't compared).
--   public_market_weekly (origin_country, process): a cell lost (no_coffees_left) or new (new_cell) follows from
--     its qualifying coffees; a changed cell must have its qualifying coffees changed (coffees_changed).
--   schema / not_reconciled (table 'release'): a compared column the served file lacks (missing_column:<col>, read
--     as null), a served table missing (missing_table), or the served release couldn't be read at all.
{%- set coffee_cols = ['coffee_id', 'roaster_id', 'is_listed', 'catalog_status', 'is_wholesale', 'is_blend',
                       'origin_country', 'process', 'in_stock', 'price_per_100g', 'first_seen', 'coffee_name',
                       'roast_level', 'min_price_usd', 'weeks_listed', 'last_seen'] %}
{#- weeks_listed and last_seen advance every week by design: kept for history_changed, left out of the comparison. #}
{%- set coffee_compared = coffee_cols | reject('in', ['weeks_listed', 'last_seen']) | list %}
{%- set event_cols = ['coffee_id', 'roaster_id', 'variant_id', 'run_date', 'event', 'price_usd',
                      'previous_price_usd', 'available'] %}
{%- set roaster_cols = ['roaster_id', 'roaster_name', 'city', 'state', 'platform', 'status', 'coffees_listed',
                        'coffees_in_stock'] %}
{%- set cell_cols = ['origin_country', 'process', 'coffees', 'roasters', 'in_stock', 'median_price_per_100g',
                     'p10_price_per_100g', 'p90_price_per_100g'] %}
{%- set tables = {'public_coffee': coffee_cols, 'public_price_event': event_cols, 'public_roaster': roaster_cols,
                  'public_market_weekly': cell_cols} %}
{%- set served = var('previous_release', {}) or {} %}
{%- set status = var('previous_release_status', '') %}
with prev_coffee as ({{ previous_release('public_coffee', coffee_cols) }}),
prev_event as ({{ previous_release('public_price_event', event_cols) }}),
prev_roaster as ({{ previous_release('public_roaster', roaster_cols) }}),
prev_cell as ({{ previous_release('public_market_weekly', cell_cols) }}),
cur_coffee as (select {{ coffee_cols | join(', ') }} from {{ ref('public_coffee') }}),
cur_event as (select {{ event_cols | join(', ') }} from {{ ref('public_price_event') }}),
cur_roaster as (select {{ roaster_cols | join(', ') }} from {{ ref('public_roaster') }}),
cur_cell as (select {{ cell_cols | join(', ') }} from {{ ref('public_market_weekly') }}),
prev_run as (
    select coalesce(cast(nullif('{{ var("previous_release_run_date", "") }}', '') as date),
                    (select max(last_seen) from prev_coffee)) as run_date
),
coffee_records as (
    select s.coffee_id, s.run_date, {{ decided('s.decision', 'p.is_coffee') }} as is_coffee, s.decision
    from {{ ref('stg_snapshots') }} s join {{ ref('stg_products') }} p using (input_hash)
),
latest as (
    select coffee_id, is_coffee, decision, min(run_date) over (partition by coffee_id) as first_record
    from coffee_records
    qualify row_number() over (partition by coffee_id order by run_date desc) = 1
),
-- Runs since the previous release in which each coffee was seen as coffee: its weekly advance of weeks_listed
-- (dim_coffee counts the same records).
advance as (
    select coffee_id, count(*) as runs from coffee_records, prev_run
    where is_coffee and coffee_records.run_date > prev_run.run_date group by coffee_id
),
history_moved as (
    select p.coffee_id from prev_coffee p join cur_coffee c using (coffee_id) left join advance a using (coffee_id)
    where p.first_seen <> c.first_seen or c.weeks_listed - p.weeks_listed <> coalesce(a.runs, 0)
),
coffee_changes as (
    select 'public_coffee' as table_name, p.coffee_id as item, 'removed' as change,
           case when not exists (select 1 from cur_roaster r where r.roaster_id = p.roaster_id) then 'roaster_left'
                when l.decision = 'exclude' then 'decision:exclude'
                when l.is_coffee = false then 'reclassified'
                else 'unexplained' end as reason
    from prev_coffee p left join latest l using (coffee_id)
    where not exists (select 1 from cur_coffee c where c.coffee_id = p.coffee_id)
    union all
    select 'public_coffee', c.coffee_id, 'added',
           case when c.first_seen > (select run_date from prev_run) then 'new'
                when not exists (select 1 from prev_roaster r where r.roaster_id = c.roaster_id) then 'roaster_joined'
                when l.decision = 'include' then 'decision:include'
                when l.first_record <= (select run_date from prev_run) then 'reclassified'
                else 'unexplained' end
    from cur_coffee c left join latest l using (coffee_id)
    where not exists (select 1 from prev_coffee p where p.coffee_id = c.coffee_id)
    union all
    select 'public_coffee', c.coffee_id, 'changed',
           case when p.is_listed and not c.is_listed then 'delisted'
                when not p.is_listed and c.is_listed then 'relisted'
                when c.coffee_id in (select coffee_id from history_moved) then 'history_changed'
                else 'attributes_changed' end
    from prev_coffee p join cur_coffee c using (coffee_id)
    where {{ row_hash('p', coffee_compared) }} <> {{ row_hash('c', coffee_compared) }}
       or c.coffee_id in (select coffee_id from history_moved)
),
removed_events as (select * from prev_event except all select * from cur_event),
added_events as (select * from cur_event except all select * from prev_event),
event_changes as (
    select 'public_price_event' as table_name, concat_ws('|', coffee_id, variant_id, run_date, event), 'removed',
           case when not exists (select 1 from cur_coffee c where c.coffee_id = e.coffee_id) then 'coffee_left'
                when coffee_id in (select coffee_id from history_moved) then 'history_changed'
                else 'unexplained' end
    from removed_events e
    union all
    select 'public_price_event', concat_ws('|', coffee_id, variant_id, run_date, event), 'added',
           case when not exists (select 1 from prev_coffee p where p.coffee_id = e.coffee_id) then 'coffee_joined'
                when run_date > (select run_date from prev_run) then 'new_run'
                when coffee_id in (select coffee_id from history_moved) then 'history_changed'
                else 'unexplained' end
    from added_events e
),
roaster_changes as (
    select 'public_roaster' as table_name, p.roaster_id, 'removed',
           case when p.roaster_id not in (select roaster_id from {{ ref('dim_coffee') }}) then 'no_coffee_left'
                when d.opted_out then 'opted_out'
                when not regexp_matches(coalesce(d.sources, ''), '(^|\+)(overture|curated|manual)') then 'source'
                when d.country is distinct from 'US' then 'outside_us'
                else 'unexplained' end
    from prev_roaster p left join {{ ref('dim_roaster') }} d using (roaster_id)
    where not exists (select 1 from cur_roaster c where c.roaster_id = p.roaster_id)
    union all
    select 'public_roaster', c.roaster_id, 'added',
           case when c.roaster_id in (select y.roaster_id from coffee_changes x join cur_coffee y on y.coffee_id = x.item
                                      where x.change = 'added') then 'first_coffee'
                else 'unexplained' end
    from cur_roaster c where not exists (select 1 from prev_roaster p where p.roaster_id = c.roaster_id)
    union all
    select 'public_roaster', c.roaster_id, 'changed', 'values_changed'
    from prev_roaster p join cur_roaster c using (roaster_id)
    where {{ row_hash('p', roaster_cols) }} <> {{ row_hash('c', roaster_cols) }}
),
-- A market cell's qualifying coffees (public_market_weekly's filter), and what each contributes.
{%- set qualifies = "is_listed and catalog_status <> 'archived' and not is_wholesale and not is_blend
      and origin_country is not null" %}
qualifying as (
    {%- for side, rel in [('prev', 'prev_coffee'), ('cur', 'cur_coffee')] %}
    select '{{ side }}' as side, origin_country, coalesce(process, 'unknown') as process,
           md5(string_agg(concat_ws('|', coffee_id, roaster_id, in_stock, price_per_100g), ',' order by coffee_id))
               as contents
    from {{ rel }} where {{ qualifies }} group by all
    {{ 'union all' if not loop.last }}
    {%- endfor %}
),
cell_changes as (
    select 'public_market_weekly' as table_name, p.origin_country || '|' || p.process, 'removed',
           case when not exists (select 1 from qualifying q where q.side = 'cur'
                                 and q.origin_country = p.origin_country and q.process = p.process)
                then 'no_coffees_left' else 'unexplained' end
    from prev_cell p
    where not exists (select 1 from cur_cell c where c.origin_country = p.origin_country and c.process = p.process)
    union all
    select 'public_market_weekly', c.origin_country || '|' || c.process, 'added',
           case when not exists (select 1 from qualifying q where q.side = 'prev'
                                 and q.origin_country = c.origin_country and q.process = c.process)
                then 'new_cell' else 'unexplained' end
    from cur_cell c
    where not exists (select 1 from prev_cell p where p.origin_country = c.origin_country and p.process = c.process)
    union all
    select 'public_market_weekly', c.origin_country || '|' || c.process, 'changed',
           case when qp.contents is distinct from qc.contents then 'coffees_changed' else 'unexplained' end
    from prev_cell p
    join cur_cell c using (origin_country, process)
    left join qualifying qp on qp.side = 'prev' and qp.origin_country = c.origin_country and qp.process = c.process
    left join qualifying qc on qc.side = 'cur' and qc.origin_country = c.origin_country and qc.process = c.process
    where {{ row_hash('p', cell_cols) }} <> {{ row_hash('c', cell_cols) }}
),
-- What couldn't be compared in full: the build goes on, and release_changes_explained warns.
coverage as (
    select null::varchar as table_name, null::varchar as item, null::varchar as change, null::varchar as reason
    where false
    {%- if status.startswith('unreadable') %}
    union all select 'release', '{{ var("baseline_build", "") }}', 'not_reconciled', '{{ status | replace("'", "''") }}'
    {%- endif %}
    {%- if served %}
    {%- for t, cols in tables.items() %}
    {%- if t not in served %}
    union all select '{{ t }}', '{{ t }}', 'schema', 'missing_table'
    {%- else %}
    {%- for c in previous_release_missing(t, cols) %}
    union all select '{{ t }}', '{{ c }}', 'schema', 'missing_column:{{ c }}'
    {%- endfor %}
    {%- endif %}
    {%- endfor %}
    {%- endif %}
)
select * from (
    select * from coffee_changes
    union all select * from event_changes
    union all select * from roaster_changes
    union all select * from cell_changes
)
-- Only the tables the served release has (nothing served: the first release, or a lake from before releases).
where {{ "table_name in ('" ~ (served.keys() | join("', '")) ~ "')" if served else 'false' }}
union all
select * from coverage
