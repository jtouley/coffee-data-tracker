-- One row per place of the discovery source list (raw candidates): its newest name, for telling a product's vendor
-- from the store's own name (screen_rules.is_own), and its tags and store count as context. Ties in added_at are
-- broken by the value itself, so the same lake gives the same row.
select roaster_id,
    arg_max(name, (added_at, name)) as name,
    arg_max(tags, (added_at, tags)) as tags,
    max(locations) as locations
from {{ lake('candidates') }}
where roaster_id is not null
group by roaster_id
