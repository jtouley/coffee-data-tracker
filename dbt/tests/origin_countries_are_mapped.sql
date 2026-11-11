-- Every origin a coffee in dim_coffee was captured with resolves (int_origin_country): to one ISO 3166-1 country, or
-- to a kind the origin_country_iso seed lists as not one country (not_a_country, ambiguous) or a list of several
-- (multiple). A new spelling fails the build until it is added to the seed, so a misspelt or unknown country is
-- never published as its own country, and never guessed. One row per unmapped spelling, with how many coffees carry it.
select c.origin_country_raw, count(*) as coffees
from {{ ref('dim_coffee') }} c
left join {{ ref('int_origin_country') }} iso on iso.raw_value = c.origin_country_raw
where c.origin_country_raw is not null and coalesce(iso.kind, 'unmapped') = 'unmapped'
group by 1
