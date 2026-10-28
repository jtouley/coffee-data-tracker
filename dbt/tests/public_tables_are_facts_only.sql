-- Guardrail: the public tables must never expose these columns. Returns a row per offending column.
-- The refs make dbt run this after every public table exists; without them it could run first and check nothing.
-- depends_on: {{ ref('public_coffee') }}, {{ ref('public_roaster') }}, {{ ref('public_price_event') }}
-- depends_on: {{ ref('public_market_weekly') }}
select table_name, column_name
from information_schema.columns
where table_name like 'public\_%' escape '\'
  and regexp_matches(column_name, '^(image|body|body_text|body_hash|description|handle|product_id|tags|vendor|'
                                  || 'shot_id|notes|rating|grinder_setting|dose_g|yield_g|user_id|photo_id|'
                                  || 'bag_id|grinder_id|machine_id|basket_id|read_json|sha256|taste|time_s|brewed_at|'
                                  || 'equipment_id)$')
