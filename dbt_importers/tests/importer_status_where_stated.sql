-- A lot whose page states its status has one: Cafe Imports' Location, InterAmerican's Status, a Royal title ending in
-- "SPOT <WAREHOUSE>" after its lot ID; and a Royal lot whose variants say whether they're available has is_available.
-- Returns a row, and fails, for each lot whose stated status or availability was lost.
with cells as (
    select
        week,
        importer,
        "row",
        max(cell_text) filter (where column_name = 'Location') as ci_location,
        max(cell_text) filter (where column_name = 'Status') as ia_status,
        max(cell_text) filter (where column_name = 'title') as title,
        max(cell_text) filter (where column_name = 'variants') as variants
    from {{ ref('stg_importer_cells') }}
    group by all
)

select o.week, o.importer, o."row", o.status, o.is_available
from {{ ref('importer_offers') }} as o
inner join cells as c using (week, importer, "row")
where
    (o.status is null and (
        (o.importer = 'cafe_imports' and {{ strip("coalesce(c.ci_location, '')") }} != '')
        or (o.importer = 'interamerican' and {{ strip("coalesce(c.ia_status, '')") }} != '')
        or (o.importer = 'royal_coffee' and regexp_matches(c.title, '\s-\s\d{5}(-\d{1,2})?\s-\s(.*\s-\s)?SPOT\s+[A-Z][A-Z0-9]{3,}(\s+[A-Z][0-9])?\s*$'))
    ))
    or (o.is_available is null and o.importer = 'royal_coffee' and regexp_matches(c.variants, '"available":\s*(true|false)'))
