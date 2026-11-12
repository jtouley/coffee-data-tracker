-- Every stored lot, every week, as an offer: its keys, the fields its page states, and our model's normalized
-- values, all derived from raw at this build (nothing derived is stored in the importers dataset). Private: only this
-- project's database file holds it.
--   Cafe Imports: its table's columns (int_importer_stated) and int_importer_rules.
--   InterAmerican: its table has no origin, region or process column, so its product name's (int_importer_name_rules)
--     fill them, with grade and screen; the rest as Cafe Imports.
--   Royal Coffee: its title is the name; everything else is int_royal_rules', from its title, tags and labelled text.
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
)

select
    l.week,
    l.fetched_at,
    l.importer,
    l."row",
    l.url,
    coalesce(s.lot_id, y.lot_id) as lot_id,
    s.name,
    coalesce(s.origin, n.origin, y.origin) as origin,
    coalesce(s.region, n.region, y.region) as region,
    coalesce(s.process, n.process, y.process) as process,
    n.grade,
    n.screen,
    s.status,
    s.est_arrival,
    coalesce(s.warehouse, y.warehouse) as warehouse,
    s.bags,
    s.bag_size,
    s.bag_kg,
    if(l.importer = 'royal_coffee', y.process_type, r.process_type) as process_type,
    if(l.importer = 'royal_coffee', y.varieties, r.varieties) as varieties,
    if(l.importer = 'royal_coffee', y.altitude_min_m, r.altitude_min_m) as altitude_min_m,
    if(l.importer = 'royal_coffee', y.altitude_max_m, r.altitude_max_m) as altitude_max_m,
    if(l.importer = 'royal_coffee', y.harvest_months, r.harvest_months) as harvest_months,
    r.grower_type,
    r.grower_name,
    if(l.importer = 'royal_coffee', y.certifications, r.certifications) as certifications,
    -- Royal never marks a certification absent: one it doesn't claim is unknown, never "not certified"
    r.not_certified,
    (
        select value from vocab
        where kind = 'rules_version'
            and term = if(l.importer = 'royal_coffee', 'royal_rules_version', 'rules_version')
    ) as rules_version,
    s.problems
from {{ ref('stg_importer_lots') }} as l
inner join {{ ref('int_importer_stated') }} as s using (week, importer, "row")
inner join {{ ref('int_importer_rules') }} as r using (week, importer, "row")
left join {{ ref('int_importer_name_rules') }} as n using (week, importer, "row")
left join {{ ref('int_royal_rules') }} as y using (week, importer, "row")
