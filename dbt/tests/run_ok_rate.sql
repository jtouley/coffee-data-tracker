-- Enough of the run succeeded: a proxy outage or a blocked runner fails most fetches at once.
{% if var('as_of_run') %}
select run_date, avg(ok::int) as ok_share
from {{ ref('run_completeness') }}
group by run_date
having avg(ok::int) < {{ var('min_ok_share') }}
{% else %}
select 1 as skipped where false
{% endif %}
