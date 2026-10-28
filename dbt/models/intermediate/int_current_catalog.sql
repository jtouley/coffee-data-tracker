-- The current catalog, as on the site: coffees listed now, without the roasters' archived back catalogs. The
-- volume metrics (mart_build_profile) and the market summary (mart_market) count it.
select * from {{ ref('dim_coffee') }} where is_listed and catalog_status <> 'archived'
