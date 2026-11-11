-- Roasters eligible for public release:
--   * not opted out
--   * has contributed at least one coffee (the registry also holds roasters never fetched)
--   * known from a source other than OpenStreetMap (Overture, the curated seed, or a manual add). Roasters known
--     *only* from OpenStreetMap (ODbL) are excluded. The filter is per roaster, not per field (#90).
--   * in the US: the dataset covers US roasters (contracts/public-catalog.odcs.yaml, coffee-data.odps.yaml). A
--     store outside it (stg_registry.country, a hand-checked seed correction) stays in the lake and the internal
--     marts, never in the public tables, which all follow this one (public_coffee joins it).
--   * its store's canonical id: an alias (dim_roaster.status 'alias', another roaster_id serving the same store)
--     has no coffee in dim_coffee, so it is left out with its coffees and price events. Its products are the
--     canonical id's own (the same store's product ids), so no history moves across;
--     tests/public_roasters_unique_by_host.sql holds the line.
select
    roaster_id, roaster_name, city, state, platform, status, coffees_listed, coffees_in_stock, last_ok_run,
    {{ release_columns() }}
from {{ ref('dim_roaster') }}
where not opted_out
  and roaster_id in (select roaster_id from {{ ref('dim_coffee') }})
  and {{ is_publishable('sources', 'country') }}
