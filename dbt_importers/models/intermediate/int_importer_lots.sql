-- Each stored lot's stable key across weeks (lot_key) and whether it was offered that week (is_available). One row per
-- stored lot, every week: raw is append-only and nothing here drops a row.
--   lot_key: the importer's own stable key, prefixed with the importer. Royal: its Shopify product id (Royal's lot ID
--     repeats across products, a full bag and a sample box of one lot, so a Royal key is one product). Cafe Imports:
--     its lot ID, else its name. InterAmerican: its table has no ID, so its product name, warehouse and bag size
--     (only as stable as the page's wording). Any of them missing: the row. A key two rows of one week share gets an
--     ordinal in page order ("#2") on the second.
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
            else nullif(concat_ws('|', lower(s.name), lower(s.warehouse), lower(s.bag_size)), '')
        end as natural_key,
        case when l.importer = 'royal_coffee' then y.available when s.bags is not null then s.bags > 0 end
            as is_available
    from {{ ref('stg_importer_lots') }} as l
    inner join {{ ref('int_importer_stated') }} as s using (week, importer, "row")
    left join {{ ref('int_royal_rules') }} as y using (week, importer, "row")
),

keyed as (
    select
        *,
        row_number() over (partition by week, importer, natural_key order by "row") as ordinal
    from lots
)

select
    week,
    importer,
    "row",
    -- two rows of one week with the same key (a lot ID listed twice, identical InterAmerican rows) are told apart in
    -- page order: the first keeps the key, the next get "#2", "#3"
    importer || ':' || coalesce(natural_key, 'row:' || "row")
    || if(natural_key is not null and ordinal > 1, '#' || ordinal, '') as lot_key,
    is_available
from keyed
