-- Warn only: every origin a coffee in dim_coffee was captured with resolves (int_origin_country): to one ISO 3166-1
-- country, or to a kind the origin_country_iso seed lists as not one country (not_a_country, ambiguous) or a list of
-- several (multiple). An unmapped spelling is published with no country and no code, never guessed, and doesn't
-- hold back a release; the warning reaches triage (coffeedata.prod_health, build:test:origin_countries_are_mapped),
-- which opens an issue to label and add it. One row per unmapped spelling, with how many coffees carry it.
{{ config(severity='warn') }}
select c.origin_country_raw, count(*) as coffees
from {{ ref('dim_coffee') }} c
left join {{ ref('int_origin_country') }} iso on iso.raw_value = c.origin_country_raw
where c.origin_country_raw is not null and coalesce(iso.kind, 'unmapped') = 'unmapped'
group by 1
