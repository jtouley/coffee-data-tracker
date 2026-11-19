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
| `coffee` | one row per coffee ever seen | name, roaster, link, origin, process, varietals, elevation, roast level, tasting notes, flavor categories, blend/decaf, bag sizes, min price, price per 100 g, `is_listed`, `in_stock`, `catalog_status` (available, preorder, sold_out, archived, delisted), `published_at` (the store's latest publish time; Shopify only), `is_wholesale` (a bulk or cafe listing, often a retail coffee's wholesale twin), first/last seen |
| `price_events` | coffee × bag × week × event | `new`, `price`, `availability`, `removed`, `relisted` with old and new price |
| `market_weekly` | origin (ISO 3166-1 name and code) × process | coffees, roasters, in stock, median / p10 / p90 price per 100 g |
| `roasters` | one row per roaster | name, city, state, storefront platform, status, coffees listed (its current retail catalog: listed, not archived, not wholesale), coffees in stock |

`manifest.json` says which release the export is (`release_build_id`, `release_run_date`), its contract version and
data licence, and each file's sha256 and row count. Each weekly release is tagged `release-<run date>` (the Monday
run), so a past week can be read with `@release-2026-11-02` after the repo id.

```sql
-- DuckDB, no download needed
select origin_country, process, coffees, median_price_per_100g
from 'hf://datasets/jtouley/us-coffee-market/data/public_market_weekly.parquet' order by coffees desc limit 10;
```

## Not included

- Product descriptions and images. They're the roasters' own content; every coffee links back through `source_url`.
- Store-internal ids other than the product id in `coffee_id` (`<roaster domain>:<store product id>`, stable across weeks).
- Personal data, and roasters that opted out.
- Roasters known only from OpenStreetMap, and any value OpenStreetMap alone gave: a roaster's name, city and state
  come from Overture, the curated seed or a manual add.
- Chains: roasters with more than 20 stores (or marked as chains by hand), and their coffees. They are still read
  each week; they are left out of the public data.

## Method and caveats

- Source: each roaster's own public storefront feed, read once a week (Shopify, WooCommerce, Squarespace, Square Online, or schema.org markup), following robots.txt.
- Coverage: only roasters whose storefront publishes a readable catalog, about half of the US roasters whose sites respond. Treat counts as a sample, not a census.
- Attributes: origin, process, notes and the rest come from structured tags and text rules, with Claude filling gaps (`attributes_source` = `rules` or `llm`). They can be wrong; the roaster's listing is authoritative.
- Origin: `origin_country` is the ISO 3166-1 English short name (so 'Tanzania, United Republic of', 'Viet Nam', "Côte d'Ivoire"; Puerto Rico is its own country) and `origin_country_code` its alpha-2 code. Both are empty when the listing names no single country: a blend's or multi-origin offer's list, a continent, or New Guinea, which two countries share.
- Prices: USD as listed, before discounts and shipping. A price the store states in another currency is never published, and roasters outside the US aren't in the public tables. `price_per_100g` uses the cheapest in-stock bag of 150 g to 2.5 kg.

## License and attribution

The dataset is released under the [Community Data License Agreement – Permissive 2.0](https://github.com/jtouley/coffee-data-tracker/blob/main/licenses/CDLA-Permissive-2.0.txt)
(CDLA-Permissive-2.0); the code that builds it is MIT. The same licence covers these tables, the site's catalog
JSON and the marketplace shares. Roaster discovery uses Overture Maps Places (© Overture Maps Foundation,
CDLA-Permissive-2.0); roasters known only from OpenStreetMap are left out. Coffee names and prices are facts
published by each roaster. See [ATTRIBUTION](https://github.com/jtouley/coffee-data-tracker/blob/main/docs/ATTRIBUTION.md).
Roasters can opt out via the [repository's issue form](https://github.com/jtouley/coffee-data-tracker/issues/new/choose).

## Citation

If you use this dataset, please cite it, with the `release_run_date` of the release you used:

> Touleyrou, J. registry.coffee: US specialty coffee market dataset (weekly release of YYYY-MM-DD).
> https://registry.coffee. Hugging Face: https://huggingface.co/datasets/jtouley/us-coffee-market.
> CDLA-Permissive-2.0.

```bibtex
@misc{registry_coffee,
  author       = {Touleyrou, Jason},
  title        = {registry.coffee: US specialty coffee market dataset},
  howpublished = {\url{https://huggingface.co/datasets/jtouley/us-coffee-market}},
  url          = {https://registry.coffee},
  note         = {Weekly releases; licensed under CDLA-Permissive-2.0}
}
```

[`CITATION.cff`](https://github.com/jtouley/coffee-data-tracker/blob/main/CITATION.cff) in the repository has the same citation.
