-- Freshness (warn): with var('today') set, the newest event is at most 3 days old, so a collector that stopped
-- writing shows. Warn only: a quiet week on a small site is possible; it becomes an error once the collector has
-- run 8 weeks and its quietest day is known (docs/usage/README.md).
{{ config(severity='warn') }}
{%- if var('today') %}
select max(received_date) as newest
from {{ ref('stg_usage_events') }}
having max(received_date) is null or max(received_date) < date '{{ var('today') }}' - 3
{%- else %}
select null as newest where false
{%- endif %}
