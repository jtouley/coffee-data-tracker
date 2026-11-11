-- Error: every alias points at a roaster that is its own canonical id. An alias_of naming an unknown roaster_id, or
-- another alias (a chain), would leave the store with no public id, or with one the crawl never reads.
select a.roaster_id, a.canonical_roaster_id, c.canonical_roaster_id as target_canonical, c.status as target_status
from {{ ref('dim_roaster') }} a
left join {{ ref('dim_roaster') }} c on c.roaster_id = a.canonical_roaster_id
where a.roaster_id <> a.canonical_roaster_id
  and (c.roaster_id is null or c.canonical_roaster_id <> c.roaster_id)
