-- A resolved host as one store's key: lower case, without a port or a trailing dot, and without the leading
-- www/www2/locations/shop/store label (registry.normalize_domain's rule, so www.Jonescoffeeroasters.com and
-- jonescoffeeroasters.com are one key). registry.HOST_KEY_SQL is the same expression for the crawl's STATE_SQL.
{% macro host_key(host) -%}
regexp_replace(rtrim(split_part(lower({{ host }}), ':', 1), '.'), '^(www\d?|locations|shop|store)\.', '')
{%- endmacro %}
