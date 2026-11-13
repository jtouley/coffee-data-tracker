-- The refs below sit inside a var check, so dbt would see them only when the build's vars are set; this line
-- declares them whatever the vars (docs/dbt/CONVENTION.md, section 5). The test itself is unchanged.
-- depends_on: {{ ref('run_completeness') }}
-- One platform's total outage must fail the run even when the rest pull the overall share above min_ok_share: each
-- adapter with at least min_adapter_roasters roasters that have a catalog must fetch min_adapter_ok_share of them
-- (with products). Smaller adapters are too noisy to judge alone; run_ok_rate still counts them. attempted_only:
-- as in run_ok_rate (`coffeedata fetch-health` judges each platform on the roasters its crawl passes have reached).
{% if var('as_of_run') %}
select adapter, count(*) as roasters, avg(catalog_ok::int) as ok_share
from {{ ref('run_completeness') }}
where has_catalog{% if var('attempted_only') %} and attempted{% endif %}
group by adapter
having count(*) >= {{ var('min_adapter_roasters') }} and avg(catalog_ok::int) < {{ var('min_adapter_ok_share') }}
{% else %}
select 1 as skipped where false
{% endif %}
