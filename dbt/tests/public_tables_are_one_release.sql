-- The refs below sit inside a var check, so dbt would see them only when the build's vars are set; this line
-- declares them whatever the vars (docs/dbt/CONVENTION.md, section 5). The test itself is unchanged.
-- depends_on: {{ ref('public_coffee') }}
-- depends_on: {{ ref('public_roaster') }}
-- depends_on: {{ ref('public_price_event') }}
-- depends_on: {{ ref('public_market_weekly') }}
-- Every row of every public table comes from this build and the run it checked: release_build_id and
-- release_run_date each take exactly one value per table, the build's (vars build_id and as_of_run), so a stale or
-- mixed release can't be served. One row per table and value that isn't this build's. Set by `coffeedata build`;
-- a plain `dbt build` has no build to compare with.
{% if var('build_id') and var('as_of_run') %}
{%- set tables = ['public_coffee', 'public_roaster', 'public_price_event', 'public_market_weekly'] %}
{%- for t in tables %}
select '{{ t }}' as table_name, release_build_id, release_run_date, count(*) as rows
from {{ ref(t) }}
where release_build_id is distinct from '{{ var("build_id") }}'
   or release_run_date is distinct from cast('{{ var("as_of_run") }}' as date)
group by all
{% if not loop.last %}union all{% endif %}
{%- endfor %}
{% else %}
select 1 as skipped where false
{% endif %}
