-- One row per address range of the pinned DB-IP "IP to City Lite" file (var('geo_db'), config/geo.lock), as
-- src/coffeedata/geo.py prepared it: ip_start and ip_end as 32 hex digits (IPv4 IPv4-mapped).
select ip_start, ip_end, country, region, city
from {{ geo_file('geo_db', ['ip_start', 'ip_end', 'country', 'region', 'city']) }}
