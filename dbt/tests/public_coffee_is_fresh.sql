-- The public catalog reflects the run the build checked: its newest last_seen is that run (as_of_run).
-- latest_run_is_fresh checks the run is recent; this checks the served table caught up with it. Older means the
-- run listed no public coffee (its fetches failed, or never landed); newer means the lake holds a later run than the
-- one the build names (`build --run-date` an older run), so the release would claim the wrong run. An empty table
-- has no run to compare and passes: an emptied catalog is the volume checks' to catch (catalog_volume_vs_last_build).
{% if var('as_of_run') %}
select max(last_seen) as newest_last_seen, cast('{{ var("as_of_run") }}' as date) as release_run
from {{ ref('public_coffee') }}
having count(*) > 0 and max(last_seen) is distinct from cast('{{ var("as_of_run") }}' as date)
{% else %}
select 1 as skipped where false
{% endif %}
