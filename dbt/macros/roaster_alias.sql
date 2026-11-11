{#- One store, one roaster_id: the rule, written once. dbt calls these macros (int_roaster_state, the host tests) and the
    crawl's registry.STATE_SQL renders this same file with jinja2 (registry.alias_macros), so the crawl skips exactly
    what dbt calls an alias. Plain SQL only: no ref(), var() or other dbt context in here.
    tests/test_registry.py checks that the SQL host key equals registry.host_key; tests/test_dbt.py checks that
    Python and dbt give every roaster the same status and canonical id. -#}

{#- A resolved host as one store's key: registry.normalize_domain's rule (lower case; no scheme, path, query, port or
    surrounding dots; no leading www/www2/locations/shop/store label), so www.Jonescoffeeroasters.com:443 and
    jonescoffeeroasters.com are one key. -#}
{% macro host_key(host) -%}
regexp_replace(trim(split_part(split_part(split_part(regexp_replace(lower(trim({{ host }})), '^[a-z]+://', ''), '/', 1), '?', 1), ':', 1), '.'), '^(www\d?|locations|shop|store)\.', '')
{%- endmacro %}

{#- The key is, or is under, a host many unrelated domains resolve to (config/shared_hosts.csv: social and ordering
    platforms, domain parking and for-sale pages, link shorteners, directories). Sharing one is no evidence of one
    store, so such a key never groups. -#}
{% macro is_shared_host(key, shared_hosts) -%}
exists (select 1 from {{ shared_hosts }} sh where {{ key }} = sh.host or ends_with({{ key }}, '.' || sh.host))
{%- endmacro %}

{#- Known from a source the public tables may list (public_roaster's rule), and in the US. -#}
{% macro is_publishable(sources, country) -%}
(regexp_matches(coalesce({{ sources }}, ''), '(^|\+)(overture|curated|manual)') and coalesce({{ country }}, 'US') = 'US')
{%- endmacro %}

{#- Each roaster's canonical id, over `base` (one row per roaster: roaster_id, host, opted_out, alias_of, sources,
    country, first_added, first_catalog_run) and `shared_hosts` (a relation with a `host` column).

    Grouping never reads the crawl status, so a store that is down, reads no coffee for a week, or is parked for a
    run keeps its aliases: the group changes only when a later successful probe resolves a member to another host
    (host is the last ok probe's), or by hand (alias_of, opted_out, sources). Every roaster groups whatever its
    status, except one whose id or host key is a shared host. A dropped roaster (not a roaster: NOT_A_ROASTER_SITE,
    the admin's flags) is removed from `base` before this runs, so it never groups, as alias or canonical.

    The canonical id of a group, in order:
      1. an opted-out one: an opt-out covers the whole store, so its other domains become aliases, never crawled or
         published;
      2. a publishable one (is_publishable), so an OSM-only id never hides a publishable store;
      3. the one whose catalog was read first (first_catalog_run, which only ever moves earlier: history): the id
         already published keeps the store, so a domain found later, read once or not at all, never takes it over;
      4. the id equal to the key; 5. one whose first label is the key's (unionroasters.co for unionroasters.com);
      6. a curated one; 7. the earliest added (null: from the seed alone, so the oldest); 8. the first by name.
    A hand-checked alias_of (a rebrand whose old domain doesn't redirect) overrides the host: the roaster joins the
    store of the roaster it names. -#}
{% macro roaster_canonical(base, shared_hosts) -%}
with host_groups as (
    select roaster_id,
           first_value(roaster_id) over (
               partition by k
               order by opted_out desc, {{ is_publishable('sources', 'country') }} desc,
                        first_catalog_run asc nulls last, roaster_id = k desc,
                        split_part(roaster_id, '.', 1) = split_part(k, '.', 1) desc,
                        regexp_matches(coalesce(sources, ''), '(^|\+)curated') desc, first_added asc nulls first,
                        roaster_id
           ) as host_canonical
    from (select *, {{ host_key('host') }} as k from {{ base }}) h
    where alias_of is null
      and not {{ is_shared_host('k', shared_hosts) }}
      and not {{ is_shared_host(host_key('roaster_id'), shared_hosts) }}
)
-- alias_of names a roaster; its store's canonical id is that roaster's (an alias_of target that is itself an alias of
-- its host group never makes a chain)
select b.roaster_id, coalesce(t.host_canonical, b.alias_of, g.host_canonical, b.roaster_id) as canonical_roaster_id
from {{ base }} b
left join host_groups g on g.roaster_id = b.roaster_id
left join host_groups t on t.roaster_id = b.alias_of
{%- endmacro %}
