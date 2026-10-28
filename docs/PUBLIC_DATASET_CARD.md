---
pretty_name: US Specialty Coffee Market (weekly)
license: cdla-permissive-2.0
tags: [coffee, retail, prices, time-series, market-data]
size_categories: [10K<n<100K]
configs:
  - config_name: coffee
    data_files: data/public_coffee.parquet
    default: true
  - config_name: price_events
    data_files: data/public_price_event.parquet
  - config_name: market_weekly
    data_files: data/public_market_weekly.parquet
  - config_name: roasters
    data_files: data/public_roaster.parquet
---

# US Specialty Coffee Market (weekly)

What US specialty coffee roasters sell online, refreshed every week: coffees, origin, process, varietals,
roast level, tasting notes, bag sizes, prices, stock, and every price, stock and listing change since tracking
began. Built by [coffee-data-tracker](https://github.com/jtouley/coffee-data-tracker).

| Table | Grain | What's in it |
|---|---|---|
| `coffee` | one row per coffee ever seen | name, roaster, link, origin, process, varietals, elevation, roast level, tasting notes, flavor categories, blend/decaf, bag sizes, min price, price per 100 g, `is_listed`, `in_stock`, `catalog_status` (available, preorder, sold_out, archived, delisted), `published_at` (the store's latest publish time; Shopify only), first/last seen |
| `price_events` | coffee × bag × week × event | `new`, `price`, `availability`, `removed`, `relisted` with old and new price |
| `market_weekly` | origin × process | coffees, roasters, in stock, median / p10 / p90 price per 100 g |
| `roasters` | one row per roaster | name, city, state, storefront platform, status, coffees listed (its current catalog: listed, not archived), coffees in stock |

`manifest.json` gives the run date and row counts of the current export.

```sql
-- DuckDB, no download needed
select origin_country, process, coffees, median_price_per_100g
from 'hf://datasets/<owner>/<name>/data/public_market_weekly.parquet' order by coffees desc limit 10;
```

## Not included

- Product descriptions and images. They're the roasters' own content; every coffee links back through `source_url`.
- Store-internal ids other than the product id in `coffee_id` (`<roaster domain>:<store product id>`, stable across weeks).
- Personal data, and roasters that opted out.
- Roasters known only from OpenStreetMap, so ODbL share-alike terms don't apply.

## Method and caveats

- Source: each roaster's own public storefront feed, read once a week (Shopify, WooCommerce, Squarespace, Square Online, or schema.org markup), following robots.txt.
- Coverage: only roasters whose storefront publishes a readable catalog, about half of the US roasters whose sites respond. Treat counts as a sample, not a census.
- Attributes: origin, process, notes and the rest come from structured tags and text rules, with Claude filling gaps (`attributes_source` = `rules` or `llm`). They can be wrong; the roaster's listing is authoritative.
- Prices: USD as listed, before discounts and shipping. `price_per_100g` uses the cheapest in-stock bag of 150 g to 2.5 kg.

## License and attribution

The compilation is released under CDLA-Permissive-2.0. Roaster discovery uses Overture Maps Places
(© Overture Maps Foundation, CDLA-Permissive-2.0). Coffee names and prices are facts published by each roaster.
Roasters can opt out via the [repository's issue form](https://github.com/jtouley/coffee-data-tracker/issues/new/choose).
