-- One row per stored lot (importer_raw), every week: its keys and its cells as stored. A week staged and also in the
-- dataset (a publish whose response was lost) is read once, by each lot's source key: Royal's Shopify product id (its
-- position in the listing can change between two reads of a week), else the row's position on the page (Cafe Imports'
-- and InterAmerican's tables have no id every row carries). The schema file's zero rows have no importer.
with raw as (
    select
        week,
        fetched_at,
        importer,
        source_url,
        parser_version,
        "row",
        url,
        cells,
        coalesce(
            if(importer = 'royal_coffee', list_filter(cells, lambda c: c."column" = 'id')[1]."text", null),
            "row"::varchar
        ) as source_key
    from {{ importer_raw() }}
    where importer is not null
)

select week, fetched_at, importer, source_url, parser_version, "row", url, cells, source_key
from raw
qualify row_number() over (partition by week, importer, source_key order by fetched_at, "row") = 1
