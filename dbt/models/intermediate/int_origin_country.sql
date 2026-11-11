-- Every origin spelling the lake holds, as captured (the rules' in stg_products, Claude's in stg_enrichments), mapped
-- to ISO 3166-1 by the origin_country_iso seed (macros/origin_iso.sql says how lists and unknown spellings resolve).
-- The captured values stay as they are; dim_coffee publishes the ISO short name and alpha-2 code from here.
with captured as (
    select origin_country as raw_value from {{ ref('stg_products') }}
    union
    select origin_country from {{ ref('stg_enrichments') }} where method = 'llm'
)
select * from ({{ origin_iso('captured', ref('origin_country_iso')) }})
