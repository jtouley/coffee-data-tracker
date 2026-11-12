-- Every stored lot, every week, as an offer: its keys, the fields its page states, and our model's normalized
-- values, all derived from raw at this build. Private: this project's database file holds it, and the importers
-- workflow keeps the latest build's copy in the private importers dataset (derived/, importers.publish_derived),
-- which no build reads back. Every stored lot stays a row (raw is append-only and a word-for-word record of the
-- page), with its lot's key and flags: is_available (offered that week) and currently_offered (dim_importer_lot:
-- offered in its importer's latest stored week).
--   Cafe Imports: its table's columns (int_importer_stated) and int_importer_rules. Its origin column is kept as
--     written in origin_as_written, and read as the catalog's country in origin.
--   InterAmerican: its table has no origin, region or process column, so its product name's (int_importer_name_rules)
--     fill them, with grade and screen; the rest as Cafe Imports.
--   Royal Coffee: its title is the name; everything else is int_royal_rules', from its title, tags and variants.
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
),

-- a place's country, as every importer reads it (macros/catalog_country.sql)
places as (
    {{ catalog_places() }}
)

select
    l.week,
    l.fetched_at,
    l.importer,
    l."row",
    l.url,
    coalesce(s.lot_id, y.lot_id) as lot_id,
    s.name,
    -- one convention for every importer: the catalog's country, or null when the text doesn't name one
    coalesce({{ catalog_country('s.origin', 'p') }}, n.origin, y.origin) as origin,
    s.origin as origin_as_written,
    coalesce(s.region, n.region) as region,
    coalesce(s.process, n.process, y.process) as process,
    n.grade,
    n.screen,
    coalesce(s.status, y.status) as status,
    -- a coffee lot, not a class or merchandise: Royal's title has a lot ID part; the others' pages list only lots
    if(l.importer = 'royal_coffee', y.is_lot, true) as is_lot,
    k.lot_key,
    k.is_available,
    d.currently_offered,
    coalesce(s.est_arrival, y.est_arrival) as est_arrival,
    coalesce(s.warehouse, y.warehouse) as warehouse,
    s.bags,
    coalesce(s.bag_size, y.bag_size) as bag_size,
    coalesce(s.bag_kg, y.bag_kg) as bag_kg,
    if(l.importer = 'royal_coffee', y.process_type, r.process_type) as process_type,
    r.varieties,
    r.altitude_min_m,
    r.altitude_max_m,
    r.harvest_months,
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
inner join {{ ref('int_importer_lots') }} as k using (week, importer, "row")
inner join {{ ref('dim_importer_lot') }} as d on d.importer = l.importer and d.lot_key = k.lot_key
cross join places as p
