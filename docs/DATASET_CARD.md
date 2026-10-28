---
pretty_name: Coffee Data, US specialty roaster catalog
license: other
license_name: mixed
license_link: https://github.com/jtouley/coffee-data-tracker/blob/main/docs/ATTRIBUTION.md
tags: [coffee, retail, prices, time-series, duckdb]
size_categories: [10K<n<100K]
configs:
  - config_name: dim_coffee
    data_files: marts/dim_coffee.parquet
  - config_name: fct_price_event
    data_files: marts/fct_price_event.parquet
  - config_name: mart_market
    data_files: marts/mart_market.parquet
  - config_name: dim_roaster
    data_files: marts/dim_roaster.parquet
---

# Coffee Data: US specialty roaster catalog

A weekly record of what US specialty coffee roasters sell online: coffees, origins, processes, tasting notes, bag
sizes, prices and stock. Built by [coffee-data-tracker](https://github.com/jtouley/coffee-data-tracker).

## Layout

- `marts/`: tables rebuilt every week from the raw records:
  - `dim_coffee`: every coffee ever seen. `is_listed` says whether it's listed now; `catalog_status` says whether you can buy it now (`available`, `preorder`) or it's sold out, the roaster's back catalog (`archived`) or gone (`delisted`); `published_at` is the store's latest publish time (Shopify only); `is_wholesale` marks bulk or cafe listings, often a retail coffee's wholesale twin, which the site, app and market counts leave out. The key is `coffee_id = <roaster domain>:<store product id>`.
  - `fct_price_event`: new / price / availability / removed / relisted events between consecutive successful weekly fetches.
  - `mart_market`: the current retail catalog (listed, not archived, not wholesale) by origin × process, with price-per-100g distribution and the most common tasting notes.
  - `dim_roaster`: the roaster registry, with platform and status. `coffees_listed` counts its current retail catalog (listed, not archived, not wholesale); `coffees_archived` counts its archived back catalog.
- The raw weekly records (`snapshots/`, `fetches/`, `probes/`, `enrichments/` and so on). Files are only ever added, never changed. The repo README describes the layout.

```sql
-- DuckDB
select origin_country, count(*) as coffees, median(price_per_100g) as median_per_100g
from 'hf://datasets/<owner>/<name>/marts/dim_coffee.parquet'
where is_listed and not is_blend group by 1 order by 2 desc;
```

## Sources, licensing and caveats

- Roaster discovery: Overture Maps Places (CDLA-Permissive-2.0) and OpenStreetMap (© OpenStreetMap contributors, ODbL). See [ATTRIBUTION](https://github.com/jtouley/coffee-data-tracker/blob/main/docs/ATTRIBUTION.md).
- Product data is read weekly from each roaster's own public storefront feed. Names, descriptions and images belong to the roasters, and every coffee links back to its source. Roasters can opt out via the repo's issue form.
- Extracted attributes (origin, process, notes and so on) come from rules or Claude and can be wrong; the roaster's listing is authoritative.
- Coverage is limited to roasters whose storefront publishes a readable catalog, about half of the US roasters whose sites respond.
