{#- The fill-rate floors' check (tests/importer_fill_floors*.sql): `floors` is a list of [importer, field, floor].
    Each importer's latest stored week, when it holds at least 100 of its lots (the tests' hand-made weeks hold a
    few); a row for each field whose share of lots with a value is below its floor. -#}
{% macro fill_floor_failures(floors) -%}
with floors (importer, field, floor) as (
    values
    {%- for f in floors %}
        ('{{ f[0] }}', '{{ f[1] }}', {{ f[2] }}){{ ',' if not loop.last }}
    {%- endfor %}
),

latest as (
    select importer, max(week) as week from {{ ref('importer_offers') }} group by importer
),

lots as (
    select o.*
    from {{ ref('importer_offers') }} as o
    inner join latest using (importer, week)
),

shares as (
    select
        importer,
        count(*) as lots,
        count(lot_id) / count(*) as lot_id,
        count(origin) / count(*) as origin,
        count(region) / count(*) as region,
        count(process) / count(*) as process,
        count(process_type) / count(*) as process_type,
        count(status) / count(*) as status,
        count(is_available) / count(*) as is_available,
        count(est_arrival) / count(*) as est_arrival,
        count(warehouse) / count(*) as warehouse,
        count(bags) / count(*) as bags,
        count(bag_size) / count(*) as bag_size,
        count(bag_kg) / count(*) as bag_kg,
        count(certifications) / count(*) as certifications
    from lots
    group by importer
),

fills as (
    unpivot (select * exclude (lots) from shares)
    on columns(* exclude (importer))
    into name field value fill
)

select f.importer, f.field, f.floor, x.fill, s.lots
from floors as f
inner join shares as s using (importer)
left join fills as x using (importer, field)
where s.lots >= 100 and coalesce(x.fill, 0) < f.floor
{%- endmacro %}
