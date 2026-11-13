-- One row per stored lot (importer_raw), every week: its keys and its cells as stored. A week staged and also in the
-- dataset (a publish whose response was lost) is read once. The schema file's zero rows have no importer.
select week, fetched_at, importer, source_url, parser_version, "row", url, cells
from {{ importer_raw() }}
where importer is not null
qualify row_number() over (partition by week, importer, "row" order by fetched_at) = 1
