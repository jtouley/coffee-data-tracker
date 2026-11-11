-- Error (contract 2.6.0, quality rule public_roasters_unique_by_host): one store is one public roaster. Two public
-- roasters whose resolved hosts have the same key (macro host_key: case, a port, a leading www/shop/store label
-- don't count) are one store listed twice, its coffees counted twice in every roaster and market count. dim_roaster
-- makes all but one of them an alias and public_roaster leaves aliases out, so a row here is a regression in that
-- rule, not a new duplicate (near_duplicate_roasters, warn, watches for those).
select {{ host_key('d.host') }} as host_key, count(*) as roasters, list(p.roaster_id order by p.roaster_id) as roaster_ids
from {{ ref('public_roaster') }} p
join {{ ref('dim_roaster') }} d using (roaster_id)
group by 1
having count(*) > 1
