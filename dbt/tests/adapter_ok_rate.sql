-- One platform's total outage must fail the run even when the rest pull the overall share above min_ok_share: each
-- adapter with at least min_adapter_roasters roasters that have a catalog must fetch min_adapter_ok_share of them
-- (with products). Smaller adapters are too noisy to judge alone; run_ok_rate still counts them.
{% if var('as_of_run') %}
select adapter, count(*) as roasters, avg(catalog_ok::int) as ok_share
from {{ ref('run_completeness') }}
where has_catalog
group by adapter
having count(*) >= {{ var('min_adapter_roasters') }} and avg(catalog_ok::int) < {{ var('min_adapter_ok_share') }}
{% else %}
select 1 as skipped where false
{% endif %}
