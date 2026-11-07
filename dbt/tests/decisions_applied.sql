-- Every decision the build was given is applied (docs/design/admin-registry.md §4, decisions_applied; an error, and
-- never to be downgraded to pass a draft): a coffee whose latest record is decided exclude is not in dim_coffee, and
-- one decided include is. stg_snapshots.decision is the decision for the record (the coffee's own, else its
-- version's); this checks dim_coffee followed it.
with latest as (
    select coffee_id, decision from {{ ref('stg_snapshots') }}
    qualify row_number() over (partition by coffee_id order by run_date desc) = 1
)
select coffee_id, decision from latest
where (decision = 'exclude' and coffee_id in (select coffee_id from {{ ref('dim_coffee') }}))
   or (decision = 'include' and coffee_id not in (select coffee_id from {{ ref('dim_coffee') }}))
