{#- The anomaly bands (build_metrics_in_band, fill_and_freshness_in_band): the metrics of mart_build_profile that
    `banded` (a condition on r.metric) selects, each against median +/- band_k x 1.4826 x MAD of its values in the
    recent passing builds since the last reset, or band_min_*_halfwidth when that is wider. The volume metrics are
    judged both sides, every other metric (a share) only below. One row per metric out of band. The rules are in
    tests/build_metrics_in_band.sql. -#}
{% macro metrics_out_of_band(banded) -%}
{%- set volume = ['listed_coffees', 'in_stock_coffees', 'roasters_with_coffee'] %}
{%- if var('as_of_run') %}
with this_run as (select cast('{{ var("as_of_run") }}' as date) as run_date),
passing as (
    select b.build_id, b.run_date, b.built_at
    from {{ ref('stg_builds') }} b, this_run
    where b.passed and b.run_date <= this_run.run_date
),
recorded as (
    select build_id, unique_id as metric, value
    from {{ lake('builds') }}
    where kind = 'metric' and build_id in (select build_id from passing)
),
marks as (
    select p.build_id, p.run_date, p.built_at,
           coalesce(max(r.value) filter (where r.metric = 'accepted_volume_change'), 0) > 0 as accepted,
           max(r.value) filter (where r.metric = 'accepted_overrides') as overrides
    from passing p left join recorded r using (build_id)
    group by all
),
resets as (
    -- accepted_overrides only grows (overrides are appended), so the most seen before is the one just before.
    select *, accepted or coalesce(overrides > max(overrides) over (
        order by built_at, build_id rows between unbounded preceding and 1 preceding), false) as reset
    from marks
),
current_build as (select metric, value from {{ ref('mart_build_profile') }}),
current_reset as (
    select {{ 'true' if var('accept_volume_change') else 'false' }}
        or coalesce((select value from current_build where metric = 'accepted_overrides')
                    > (select max(overrides) from marks), false) as reset
),
window_start as (select max(run_date) as run_date from resets where reset),
latest_per_run as (
    select m.build_id, m.run_date
    from marks m, this_run, window_start
    where m.run_date < this_run.run_date and (window_start.run_date is null or m.run_date >= window_start.run_date)
    qualify row_number() over (partition by m.run_date order by m.built_at desc, m.build_id desc) = 1
),
history as (
    select build_id from latest_per_run
    qualify row_number() over (order by run_date desc) <= {{ var('band_window') }}
),
banded as (
    select r.metric, r.value
    from recorded r join history using (build_id)
    where {{ banded }}
),
centre as (select metric, count(*) as builds, median(value) as median from banded group by metric),
bands as (
    select c.metric, c.builds, c.median, median(abs(b.value - c.median)) as mad,
           c.metric in ('{{ volume | join("', '") }}') as is_volume
    from centre c join banded b using (metric)
    group by all
),
judged as (
    select b.*, cur.value as current_value,
           greatest({{ var('band_k') }} * 1.4826 * b.mad, case when b.is_volume
               then {{ var('band_min_volume_halfwidth') }} * abs(b.median)
               else {{ var('band_min_share_halfwidth') }} end) as halfwidth
    from bands b
    join current_build cur using (metric)
    left join current_build n on n.metric = replace(b.metric, 'adapter_ok_share:', 'adapter_roasters:')
    where b.builds >= {{ var('band_min_builds') }}
      and (b.metric not like 'adapter_ok_share:%' or n.value >= {{ var('min_adapter_roasters') }})
)
select metric, current_value, median, mad, halfwidth, builds
from judged, current_reset
where not current_reset.reset
  and (current_value < median - halfwidth or (is_volume and current_value > median + halfwidth))
{% else %}
select 1 as skipped where false
{% endif %}
{%- endmacro %}
