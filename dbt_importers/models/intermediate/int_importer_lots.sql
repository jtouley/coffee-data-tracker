-- Each stored lot's stable key across weeks (lot_key) and whether it was offered that week (is_available). One row per
-- stored lot, every week: raw is append-only and nothing here drops a row.
--   lot_key: the importer's own stable key, prefixed with the importer. Royal: its Shopify product id (Royal's lot ID
--     repeats across products, a full bag and a sample box of one lot). Cafe Imports: its lot ID, else its name.
--     InterAmerican: its table has no ID, so its product name, warehouse and bag size, with an ordinal for the rare
--     identical rows in one week (in page order); that key is only as stable as the page's wording.
--   is_available: Royal's variants say so (Shopify's variants.available); Cafe Imports' and InterAmerican's pages
--     list the bags still available, so a lot with bags left is offered. Null when the page doesn't say.
with lots as (
    select
        l.week,
        l.importer,
        l."row",
        case l.importer
            when 'royal_coffee' then l.source_key
            when 'cafe_imports' then coalesce(s.lot_id, 'name:' || lower(s.name))
            else concat_ws('|', lower(s.name), lower(s.warehouse), lower(s.bag_size))
        end as natural_key,
        case when l.importer = 'royal_coffee' then y.available when s.bags is not null then s.bags > 0 end
            as is_available
    from {{ ref('stg_importer_lots') }} as l
    inner join {{ ref('int_importer_stated') }} as s using (week, importer, "row")
    left join {{ ref('int_royal_rules') }} as y using (week, importer, "row")
)

select
    week,
    importer,
    "row",
    importer || ':' || coalesce(natural_key, 'row:' || "row")
    || if(
        importer = 'interamerican',
        '#' || row_number() over (partition by week, importer, natural_key order by "row"),
        ''
    ) as lot_key,
    is_available
from lots
