-- run_manifests.mode is one the writers set: full, sample, only or retry_failed (ingest.select_states, crawl and
-- crawl-retry), or null in a file from before the column. An error, not a warning: run_completeness takes a run's
-- expected set from its mode = 'full' manifests, so an unknown mode silently changes what run_is_complete checks.
select mode, count(*) as manifest_rows
from {{ lake('run_manifests') }}
where roaster_id is not null and mode not in ('full', 'sample', 'only', 'retry_failed')
group by mode
