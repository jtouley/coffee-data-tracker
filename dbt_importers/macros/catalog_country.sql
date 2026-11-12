{#- One reading of a place as the catalog's country, for every importer (importer_offers, int_royal_rules), so an
    origin follows one convention. catalog_places() is a one-row CTE body over the `vocab` CTE: the catalog's
    countries and aliases with Royal's own spellings (lowercase, country_of), the places that name no one country
    (ambiguous) and the countries an origin may take (countries). catalog_country(place, p) reads `place` through
    the CTE `p`: its spacing and case don't matter; an ambiguous place, an unknown one or a blank is null. -#}
{% macro catalog_places() -%}
select
    map_from_entries(list({'key': term, 'value': value}) filter (where kind in ('country', 'royal_place')))
        as country_of,
    list(term) filter (where kind = 'ambiguous_place') as ambiguous,
    list(value) filter (where kind = 'catalog_country') as countries
from vocab
where kind in ('country', 'royal_place', 'ambiguous_place', 'catalog_country')
{%- endmacro %}

{% macro place_key(place) -%}
lower(regexp_replace({{ strip(place) }}, '{{ ws() }}+', ' ', 'g'))
{%- endmacro %}

{% macro catalog_country(place, p) -%}
(case
    when list_contains({{ p }}.ambiguous, {{ place_key(place) }}) then null
    when list_contains({{ p }}.countries, {{ p }}.country_of[{{ place_key(place) }}])
        then {{ p }}.country_of[{{ place_key(place) }}]
end)
{%- endmacro %}
