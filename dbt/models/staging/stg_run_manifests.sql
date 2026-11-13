-- One row per run manifest and roaster: what an ingest recorded, before fetching, that it set out to fetch
-- (ingest.manifest_rows). status, platform and adapter are the roaster's as that ingest saw them; mode is full,
-- sample, only or retry_failed (null in a file from before the column); started_at is when the ingest began.
select run_date, manifest_id, started_at, roaster_id, status, platform, adapter, mode
from {{ lake('run_manifests') }}
where roaster_id is not null
