-- Roasters eligible for public release:
--   * not opted out
--   * has contributed at least one coffee (the registry also holds roasters never fetched)
--   * known from a source we may redistribute without share-alike terms (Overture, the curated seed, or a
--     manual add). Roasters known *only* from OpenStreetMap are excluded: ODbL requires derived databases to
--     be shared under ODbL, which would bind the whole listing.
select
    roaster_id, roaster_name, city, state, platform, status, coffees_listed, coffees_in_stock, last_ok_run,
    {{ release_columns() }}
from {{ ref('dim_roaster') }}
where not opted_out
  and roaster_id in (select roaster_id from {{ ref('dim_coffee') }})
  and regexp_matches(coalesce(sources, ''), '(^|\+)(overture|curated|manual)')
