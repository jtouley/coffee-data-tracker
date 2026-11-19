-- The refs below sit inside a var check, so dbt would see them only when the build's vars are set; this line
-- declares them whatever the vars (docs/dbt/CONVENTION.md, section 5). The test itself is unchanged.
-- depends_on: {{ ref('stg_runs') }}
-- The catalog must come from a recent weekly run: a stopped job must not keep publishing an old catalog as current.
-- Measured against the day of the build (var today), not the run being checked.
{% if var('today') %}
select max(run_date) as latest_run
from {{ ref('stg_runs') }}
having max(run_date) < cast('{{ var("today") }}' as date) - interval {{ var('max_run_age_days') }} day
{% else %}
select 1 as skipped where false
{% endif %}
