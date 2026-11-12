-- stg_fetches, with each ok fetch's coffee count as its crawl recorded it, or, for a fetch whose crawl recorded none
-- (an ELT crawl loads without classifying), as int_fetch_coffees counts it with the same rule. What dim_roaster's
-- status reads.
select f.* replace (coalesce(f.coffees, c.coffees) as coffees)
from {{ ref('stg_fetches') }} f
left join {{ ref('int_fetch_coffees') }} c using (roaster_id, batch_id)
