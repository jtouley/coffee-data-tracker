-- Every decision the build was given is applied (docs/design/admin-registry.md §4, decisions_applied; an error, and
-- never to be downgraded to pass a draft): a coffee whose latest record is decided exclude is not in dim_coffee, and
-- one decided include is. stg_snapshots.decision is the decision for the record (the coffee's own, else its
-- version's); this checks dim_coffee followed it.
-- An alias's coffees (int_roaster_state: another roaster_id serves the same store, #137) are never in dim_coffee: the
-- canonical id's copy is the store's product. A version decision (input_hash, which has no roaster in it) reaches the
-- alias's copy too, and a coffee's own include may predate the alias being resolved, so an include is checked on the
-- canonical ids' coffees only. An exclude is checked everywhere (an alias's coffee is out either way).
with latest as (
    select coffee_id, roaster_id, decision from {{ ref('stg_snapshots') }}
    qualify row_number() over (partition by coffee_id order by run_date desc) = 1
),
aliases as (
    select roaster_id from {{ ref('int_roaster_state') }} where roaster_id <> canonical_roaster_id
)
select coffee_id, decision from latest
where (decision = 'exclude' and coffee_id in (select coffee_id from {{ ref('dim_coffee') }}))
   or (decision = 'include' and coffee_id not in (select coffee_id from {{ ref('dim_coffee') }})
       and roaster_id not in (select roaster_id from aliases))
