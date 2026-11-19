-- The IP prefix is read only to find a location (design section 4): no table or view of this project but
-- stg_usage_ip_prefixes (a view over raw, read only by int_usage_location) has a column that can hold it.
-- tests/test_usage_dbt.py checks every model's SQL the same way. Returns one row per other column.
-- depends_on: {{ ref('int_usage_location') }}, {{ ref('mart_usage_locations_weekly') }}
{{ config(severity='error') }}
select table_name, column_name
from information_schema.columns
where (regexp_matches(lower(column_name), 'ip_?prefix|^ip$|ip_addr|address|^ip_hex$'))
  and table_name <> 'stg_usage_ip_prefixes'
