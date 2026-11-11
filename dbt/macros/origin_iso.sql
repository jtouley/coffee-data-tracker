{#-
  Each distinct raw origin spelling in `raw_values` (a relation or CTE with a raw_value column), resolved to ISO 3166-1
  through `spellings` (the origin_country_iso seed). One row per spelling: raw_value, kind, alpha2, alpha3,
  iso_short_name, region. Plain SQL and Jinja only, so tests/test_origin_iso.py renders it outside dbt, over the
  labeled census, and scores the same code the build runs.

  1. The whole spelling, compared trimmed and in lower case, when the seed has it ("Trinidad and Tobago" is one
     country, never split). Its kind is the seed's: country, not_a_country (a continent or a region larger than a
     country) or ambiguous (a place more than one country shares: New Guinea).
  2. Otherwise a list ("Colombia, Ethiopia", "Colombia & Peru", "Colombia/Costa Rica", "Central America And
     Indonesia", "Kenya, Hawaii (Usa)"): split on , & / ( ) and the word "and", and each part looked up the same way.
     - Every part a country, all the same one ("Hawaii, Usa"): that country, with the parts' sub-national region
       (Hawaii).
     - Two or more countries, or a country with a region or an ambiguous place: multiple. Several origins are a blend
       or a multi-origin offer (extract.py's rule for a title naming several countries), never one country.
  3. Anything else (a single unknown spelling, or a list with an unknown part) is unmapped: no code, never a guess.
     origin_countries_are_mapped fails the build on an unmapped spelling in dim_coffee.
-#}
{% macro origin_iso(raw_values, spellings) -%}
with raw as (
    select distinct raw_value from {{ raw_values }} where raw_value is not null and trim(raw_value) <> ''
),
spelling as (
    select lower(trim(raw_value)) as k, kind, alpha2, alpha3, iso_short_name, region from {{ spellings }}
),
whole as (
    select r.raw_value, s.kind, s.alpha2, s.alpha3, s.iso_short_name, s.region
    from raw r join spelling s on s.k = lower(trim(r.raw_value))
),
parts as (
    select r.raw_value, trim(t.part) as part
    from raw r, unnest(regexp_split_to_array(r.raw_value, '(?i)\s*(?:,|&|/|\(|\)|\band\b)\s*')) as t(part)
    where r.raw_value not in (select raw_value from whole) and trim(t.part) <> ''
),
part_map as (
    select p.raw_value, s.kind, s.alpha2, s.alpha3, s.iso_short_name, s.region
    from parts p left join spelling s on s.k = lower(p.part)
),
lists as (
    select
        raw_value,
        count(*) as n_parts,
        count(*) filter (where kind is null) as n_unknown,
        count(*) filter (where kind <> 'country') as n_not_country,
        count(distinct alpha2) as n_countries,
        any_value(alpha2) as alpha2, any_value(alpha3) as alpha3, any_value(iso_short_name) as iso_short_name,
        string_agg(distinct region, ', ' order by region) as region
    from part_map group by raw_value
),
resolved as (
    select raw_value, kind, alpha2, alpha3, iso_short_name, region from whole
    union all
    select
        raw_value,
        case
            when n_parts < 2 or n_unknown > 0 then 'unmapped'
            when n_countries = 1 and n_not_country = 0 then 'country'
            else 'multiple'
        end as kind,
        alpha2, alpha3, iso_short_name, region
    from lists
)
select
    raw_value, kind,
    if(kind = 'country', alpha2, null) as alpha2,
    if(kind = 'country', alpha3, null) as alpha3,
    if(kind = 'country', iso_short_name, null) as iso_short_name,
    if(kind = 'country', region, null) as region
from resolved
{%- endmacro %}
