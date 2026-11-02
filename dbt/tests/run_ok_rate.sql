-- Enough of the run succeeded: a proxy outage or a blocked runner fails most fetches at once. Counted over the
-- roasters that have a catalog (run_completeness has_catalog), so the ~600 no_catalog stores that "succeed" with 0
-- products don't pad the share; a success has to bring back products. attempted_only (set by `coffeedata crawl-dq`,
-- which runs this test on the week's crawl output before the merge): only the roasters a crawl pass has reached,
-- so the ones a pass left for its resume slot don't count as failures. The weekly build never sets it.
{% if var('as_of_run') %}
select run_date, count(*) as roasters, avg(catalog_ok::int) as ok_share
from {{ ref('run_completeness') }}
where has_catalog{% if var('attempted_only') %} and attempted{% endif %}
group by run_date
having avg(catalog_ok::int) < {{ var('min_ok_share') }}
{% else %}
select 1 as skipped where false
{% endif %}
