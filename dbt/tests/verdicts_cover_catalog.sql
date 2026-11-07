-- Claude's verdicts ran for this release: a build without a verdict model, or with too few listed coffees decided
-- by one, fails. Until 2026-11 every release was the rules alone (about 10.7% non-coffee kept, against 2.7% with
-- verdicts) because the build job never had a model and classify skipped with exit 0. Checked only when
-- require_verdicts is set (`coffeedata build`, unless rules only is explicitly allowed); min_verdict_share and the
-- expected lag are explained in dbt_project.yml.
{% if var('require_verdicts') %}
select 'no verdict model configured' as problem, null::double as verdict_share, null::bigint as listed
where '{{ var("verdict_model") }}' = ''
union all
select 'too few listed coffees decided by a verdict' as problem, verdict_share, listed
from (
    select avg((classified_by = 'claude')::int) as verdict_share, count(*) as listed
    from {{ ref('dim_coffee') }}
    where is_listed
)
where '{{ var("verdict_model") }}' <> '' and listed > 0 and verdict_share < {{ var('min_verdict_share') }}
{% else %}
select 1 as skipped where false
{% endif %}
