-- Guardrail: the public tables must never expose these columns. Returns a row per offending column.
select table_name, column_name
from information_schema.columns
where table_name like 'public\_%' escape '\'
  and regexp_matches(column_name, '^(image|body|body_text|body_hash|description|handle|product_id|tags|vendor|'
                                  || 'shot_id|notes|rating|grinder_setting|dose_g|yield_g|user_id|photo_id|'
                                  || 'bag_id|grinder_id|machine_id|basket_id|read_json|sha256)$')
