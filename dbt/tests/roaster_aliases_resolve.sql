-- Error: every alias points at a roaster that is its own canonical id, and never hides a store the public tables
-- may list. A row is
--   * an alias whose canonical id is unknown (an alias_of naming a roaster_id the registry doesn't have), or is itself
--     an alias (a chain): the store would have no id the crawl reads;
--   * a publishable alias (macro is_publishable) whose canonical id is neither publishable nor opted out: the store
--     would be crawled under an id the public tables can't list, so it would leave them. The host rule never does
--     this (it ranks publishable ids first); a hand-set alias_of can.
-- The crawl status is not checked: a canonical id that is down or reads no coffee for a while keeps its store
-- (macros/roaster_alias.sql), and this error test must not fail a release for an outage.
select a.roaster_id, a.canonical_roaster_id, c.canonical_roaster_id as target_canonical, c.status as target_status,
       c.sources as target_sources, c.country as target_country
from {{ ref('dim_roaster') }} a
left join {{ ref('dim_roaster') }} c on c.roaster_id = a.canonical_roaster_id
where a.roaster_id <> a.canonical_roaster_id
  and (c.roaster_id is null
       or c.canonical_roaster_id <> c.roaster_id
       or ({{ is_publishable('a.sources', 'a.country') }} and not c.opted_out
           and not {{ is_publishable('c.sources', 'c.country') }}))
