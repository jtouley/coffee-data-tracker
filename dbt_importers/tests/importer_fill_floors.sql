-- Each importer's fill-rate floors on its latest stored week: the share of lots with a value, per field, may not fall
-- below the floor. Set a little below what the rules reach on the week of 2026-11-09 (eval/importers/README.md has
-- the numbers), so a page or listing that changes shape, or a rule that stops matching, fails the build instead of
-- quietly nulling a field. Floors are only ever raised. A week with fewer than 100 of an importer's lots isn't a real
-- page (the tests' hand-made weeks hold a few), so its floors don't apply. Fields whose fill follows the week's mix of
-- lots rather than the rules (Cafe Imports' arrival dates, most certifications) have low floors or none. Returns a
-- row, and fails, for each field below its floor.
with floors (importer, field, floor) as (
    values
        ('cafe_imports', 'lot_id', 0.95),
        ('cafe_imports', 'origin', 0.95),
        ('cafe_imports', 'region', 0.60),
        ('cafe_imports', 'process', 0.95),
        ('cafe_imports', 'process_type', 0.85),
        ('cafe_imports', 'status', 0.95),
        ('cafe_imports', 'is_available', 0.95),
        ('cafe_imports', 'bags', 0.95),
        ('cafe_imports', 'bag_kg', 0.95),
        ('interamerican', 'origin', 0.90),
        ('interamerican', 'region', 0.25),
        ('interamerican', 'process', 0.25),
        ('interamerican', 'process_type', 0.15),
        ('interamerican', 'status', 0.95),
        ('interamerican', 'is_available', 0.95),
        ('interamerican', 'warehouse', 0.95),
        ('interamerican', 'bags', 0.95),
        ('interamerican', 'bag_kg', 0.95),
        ('royal_coffee', 'lot_id', 0.97),
        ('royal_coffee', 'origin', 0.97),
        ('royal_coffee', 'process', 0.95),
        ('royal_coffee', 'process_type', 0.85),
        ('royal_coffee', 'status', 0.88),
        ('royal_coffee', 'is_available', 0.97),
        ('royal_coffee', 'warehouse', 0.97),
        ('royal_coffee', 'bag_size', 0.50),
        ('royal_coffee', 'bag_kg', 0.97),
        ('royal_coffee', 'est_arrival', 0.01),
        ('royal_coffee', 'certifications', 0.35)
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
