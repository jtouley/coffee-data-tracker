# Data attribution

The public dataset (the public tables, their exports, the site's catalog JSON and the marketplace shares) is
released under the [CDLA-Permissive-2.0](../licenses/CDLA-Permissive-2.0.txt); the code is under the
[MIT License](../LICENSE). It draws on the sources below. Roasters known only from OpenStreetMap are left out of
the public tables, and the roasters they do list carry no value from OpenStreetMap: each name, city and state comes
from Overture, the curated seed or a manual add (dbt test `public_values_have_open_provenance`).

- **Roaster discovery: Overture Maps Places.** © Overture Maps Foundation. Licensed under the
  [CDLA-Permissive-2.0](https://cdla.dev/permissive-2-0/). Source datasets carry their own attribution, listed in the
  Overture release notes. Registry rows with `source` starting `overture-` come from the named release.
- **Roaster discovery: OpenStreetMap.** © OpenStreetMap contributors, available under the
  [Open Database License](https://www.openstreetmap.org/copyright). Registry rows with `source` `osm` came from it.
- **Product catalogs.** Read from each roaster's own public storefront feed (Shopify `/products.json`, the WooCommerce
  Store API, Squarespace collection JSON, or schema.org Product markup). Product names, descriptions and images belong
  to the roasters; this project stores only what a shopper sees, and links every coffee back to its roaster.
- **Enrichment.** Attributes in the lake's `enrichments/` are extracted from those listings by rules, or by Claude
  (`method = 'llm'`), and can be wrong; the roaster's listing is authoritative.
