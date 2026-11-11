-- Each roaster's current state (int_roaster_state: status from probe and fetch history, and its store's canonical
-- id) with its coffee counts. An alias has none: dim_coffee leaves aliases out.
with coffees as (
    -- Listed means the current catalog; a roaster's archived back catalog is counted separately.
    -- Wholesale listings (a roaster's wholesale twin of a retail coffee) aren't counted as more coffees.
    select roaster_id,
           count(*) filter (where is_listed and catalog_status <> 'archived' and not is_wholesale) as coffees_listed,
           count(*) filter (where in_stock and not is_wholesale) as coffees_in_stock,
           count(*) filter (where catalog_status = 'archived' and not is_wholesale) as coffees_archived
    from {{ ref('dim_coffee') }} group by roaster_id
)
select
    s.roaster_id, s.roaster_name, s.city, s.state, s.country, s.locations, s.is_chain, s.opted_out, s.sources,
    s.host, s.platform, s.last_probed, s.last_ok_run, s.last_fetch, s.status, s.canonical_roaster_id,
    coalesce(c.coffees_listed, 0) as coffees_listed, coalesce(c.coffees_in_stock, 0) as coffees_in_stock,
    coalesce(c.coffees_archived, 0) as coffees_archived,
    {{ release_columns() }}
from {{ ref('int_roaster_state') }} s
left join coffees c using (roaster_id)
