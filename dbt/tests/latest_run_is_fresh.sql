-- The catalog must come from a recent weekly run: a stopped job must not keep publishing an old catalog as current.
{% if var('as_of_run') %}
select max(run_date) as latest_run
from {{ ref('stg_runs') }}
having max(run_date) < cast('{{ var("as_of_run") }}' as date) - interval {{ var('max_run_age_days') }} day
{% else %}
select 1 as skipped where false
{% endif %}
