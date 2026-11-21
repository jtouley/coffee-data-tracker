-- Report only (tag report_only): the census reconciles.
--   within: eval_prefilter_summary has exactly one row per direction (excluded, restored), its products equal to
--     Claude's three calls together, and each count equal to eval_prefilter's rows for that direction and call;
--   across tables: every product the census says a new filter takes out of the catalog (excluded, not coffee after,
--     public in the served release) is a removed row of release_changes' public_coffee.
-- Any row returned is a count that doesn't reconcile.
{{ config(tags=['report_only'], severity='warn') }}
with rows_ as (
    select direction, count(*) as products,
           count(*) filter (where claude = 'coffee') as claude_coffee,
           count(*) filter (where claude = 'not_coffee') as claude_not_coffee,
           count(*) filter (where claude = 'no_verdict') as no_verdict,
           count(*) filter (where is_coffee_after) as back_in_catalog
    from {{ ref('eval_prefilter') }}
    group by direction
)
select s.direction, 'does not reconcile' as problem
from {{ ref('eval_prefilter_summary') }} s
left join rows_ r using (direction)
where s.products <> s.claude_coffee + s.claude_not_coffee + s.no_verdict
   or s.products <> coalesce(r.products, 0)
   or s.claude_coffee <> coalesce(r.claude_coffee, 0)
   or s.claude_not_coffee <> coalesce(r.claude_not_coffee, 0)
   or s.no_verdict <> coalesce(r.no_verdict, 0)
   or s.back_in_catalog <> coalesce(r.back_in_catalog, 0)
union all
select 'summary', 'expected exactly the two directions'
from (select count(*) as n from {{ ref('eval_prefilter_summary') }}) where n <> 2
union all
select e.coffee_id, 'taken out of the catalog, but not removed in release_changes'
from {{ ref('eval_prefilter') }} e
join {{ ref('int_catalog_candidates') }} c using (coffee_id)
where e.direction = 'excluded' and not e.is_coffee_after and c.was_public
  and e.coffee_id not in (
      select item from {{ ref('release_changes') }} where table_name = 'public_coffee' and change = 'removed'
  )
