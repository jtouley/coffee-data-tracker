-- Staging drops a raw row with no event_id (it can't be deduplicated); this counts them, so the drop is never
-- silent (docs/dbt/CONVENTION.md section 5). Warn: the collector's schema requires the id, so any count is a bug
-- there; it becomes an error once the collector's contract test pins the id (design PR 1).
{{ config(severity='warn') }}
select count(*) as rows_without_event_id
from {{ usage_raw() }} as r
where event_id is null
having count(*) > 0
