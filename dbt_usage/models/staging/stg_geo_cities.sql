-- One row per (country, region, lowercased city name) of the pinned GeoNames file (var('geo_cities'),
-- config/geo.lock): places of 50,000 people or more, under every name DB-IP may use for them.
select country, region, city, cast(population as bigint) as population
from {{ geo_file('geo_cities', ['country', 'region', 'city', 'population']) }}
