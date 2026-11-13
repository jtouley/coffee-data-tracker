-- The refs below sit inside a var check, so dbt would see them only when the build's vars are set; this line
-- declares them whatever the vars (docs/dbt/CONVENTION.md, section 5). The test itself is unchanged.
-- depends_on: {{ ref('run_completeness') }}
-- Every roaster the run should have tried was tried (an error counts): a run that skipped roasters (a crash, a
-- --sample run) must not be served as a full week. Lists the roasters it skipped.
{% if var('as_of_run') %}
select run_date, roaster_id
from {{ ref('run_completeness') }}
where not attempted
  and (select avg(attempted::int) from {{ ref('run_completeness') }}) < {{ var('min_attempted_share') }}
{% else %}
select 1 as skipped where false
{% endif %}
