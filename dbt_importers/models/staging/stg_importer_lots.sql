-- One row per stored lot (importer_raw), every week: its keys and its cells as stored. A week stored twice (staged and
-- also in the dataset: a publish whose response was lost) is read once: only the week's first fetch (its earliest
-- fetched_at), so two fetches' rows never mix and no two products share a row; within it, each lot once by its source
-- key, Royal's Shopify product id, else the row's position on the page (Cafe Imports' and InterAmerican's tables have
-- no id every row carries). The schema file's zero rows have no importer.
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
),

first_fetch as (
    select *
    from raw
    qualify fetched_at = min(fetched_at) over (partition by week, importer)
)

select week, fetched_at, importer, source_url, parser_version, "row", url, cells, source_key
from first_fetch
qualify row_number() over (partition by week, importer, source_key order by "row") = 1
