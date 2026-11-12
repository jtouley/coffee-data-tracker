-- Our model's values for each lot, from its rule inputs (int_importer_inputs) and the rules' vocabulary
-- (stg_importer_vocab: importer_rules, and the catalog's process and variety words). Each rule decides only when the
-- text settles it and abstains (null) otherwise; a value we can't tell is left empty, never guessed.
--   process_type: washed, natural, honey or anaerobic, as the catalog's model has them (pulped natural, semi-washed
--     and wet-hulled count as honey). Decaf anywhere: null. Anaerobic or carbonic anywhere wins. Otherwise the
--     page's process column decides when it has one, else the name, and only when it names exactly one process.
--   varieties: the varieties the name names, in the catalog's spelling, sorted; Java is the island on an Indonesian lot.
--   altitude_min_m, altitude_max_m: only in an altitude context ("1,700-1,900 masl", "grown at 1800 m",
--     "Altitude: 1500"), within 300-3,000 m, the lower first.
--   harvest_months: from "Harvest: Nov - Feb", as whole month words; a lone "May" only in a month context.
--   grower_type, grower_name: the first " - "-separated part of the name with a grower word; a placeholder name
--     ("Fazenda TBC") keeps the type and leaves the name out.
--   certifications, not_certified: the modeled certifications claimed, and those the page marks absent (Cafe
--     Imports' "nft", "norg"); each null when there are none. Both null (unknown) when the text names a certification
--     or program we don't model (SMBC, Bird Friendly, ...) or claims and denies one.
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
),

patterns as (
    select
        max(value) filter (where kind = 'pattern' and term = 'decaf') as decaf,
        max(value) filter (where kind = 'pattern' and term = 'honey') as honey,
        max(value) filter (where kind = 'process' and term = 'anaerobic') as anaerobic,
        max(value) filter (where kind = 'process' and term = 'natural') as natural_re,
        max(value) filter (where kind = 'process' and term = 'washed') as washed,
        max(value) filter (where kind = 'pattern' and term = 'varietal') as varietal,
        max(value) filter (where kind = 'pattern' and term = 'altitude') as altitude,
        max(value) filter (where kind = 'pattern' and term = 'harvest') as harvest,
        max(value) filter (where kind = 'pattern' and term = 'packaging') as packaging,
        max(value) filter (where kind = 'pattern' and term = 'placeholder') as placeholder,
        max(value) filter (where kind = 'pattern' and term = 'other_certs') as other_certs,
        max(value) filter (where kind = 'grower_type' and term = 'micromill') as micromill,
        max(value) filter (where kind = 'grower_type' and term = 'washing_station') as washing_station,
        max(value) filter (where kind = 'grower_type' and term = 'cooperative') as cooperative,
        max(value) filter (where kind = 'grower_type' and term = 'farm') as farm,
        max(value) filter (where kind = 'certification' and term = 'organic') as organic_yes,
        max(value2) filter (where kind = 'certification' and term = 'organic') as organic_no,
        max(value) filter (where kind = 'certification' and term = 'fair_trade') as fair_trade_yes,
        max(value2) filter (where kind = 'certification' and term = 'fair_trade') as fair_trade_no,
        max(value) filter (where kind = 'certification' and term = 'rainforest_alliance') as rainforest_yes,
        max(value2) filter (where kind = 'certification' and term = 'rainforest_alliance') as rainforest_no
    from vocab
),

varietal_names as (
    select term as spelling_key, value as varietal from vocab where kind = 'varietal'
),

inputs as (
    select i.*, p.*
    from {{ ref('int_importer_inputs') }} as i
    cross join patterns as p
),

-- process
process_inputs as (
    select
        *,
        coalesce(rule_stated_process, '') || ' ' || coalesce(rule_name, '') as process_text,
        coalesce(nullif(rule_stated_process, ''), nullif(rule_name, '')) as process_source
    from inputs
),

process_type as (
    select
        week,
        importer,
        "row",
        case
            when regexp_matches(process_text, decaf, 'i') then null
            when regexp_matches(process_text, anaerobic, 'i') then 'anaerobic'
            when process_source is null then null
            else {{ process_kind('process_source', 'anaerobic', 'honey', 'natural_re', 'washed') }}
        end as process_type
    from process_inputs
),

-- varieties
variety_matches as (
    select
        week,
        importer,
        "row",
        unnest(regexp_extract_all(coalesce(rule_name, ''), varietal, 1, 'i')) as matched,
        regexp_matches(coalesce(rule_origin, '') || ' ' || coalesce(rule_name, ''), '\bindonesia', 'i')
        or lower({{ strip("coalesce(rule_origin, '')") }}) = 'java' as is_indonesian
    from inputs
),

varieties as (
    select
        m.week,
        m.importer,
        m."row",
        list_sort(list_distinct(list(coalesce(n.varietal, m.matched)))) as varieties
    from variety_matches as m
    left join varietal_names as n on n.spelling_key = regexp_replace(lower(m.matched), '[{{ ws_inner() }}-]', '', 'g')
    where not (m.is_indonesian and coalesce(n.varietal, m.matched) = 'Java')
    group by all
),

-- altitude
altitude_groups as (
    select
        week,
        importer,
        "row",
        regexp_extract(rule_text, altitude, 1, 'i') as g1,
        regexp_extract(rule_text, altitude, 2, 'i') as g2,
        regexp_extract(rule_text, altitude, 3, 'i') as g3,
        regexp_extract(rule_text, altitude, 4, 'i') as g4
    from inputs
),

altitude_metres as (
    select
        week,
        importer,
        "row",
        try_cast(replace(replace(if(g1 != '', g1, g3), ',', ''), '.', '') as integer) as lo,
        try_cast(replace(replace(coalesce(nullif(if(g1 != '', g2, g4), ''), if(g1 != '', g1, g3)), ',', ''), '.', '')
            as integer) as hi
    from altitude_groups
),

altitude as (
    select
        week,
        importer,
        "row",
        lo between 300 and 3000 and hi between 300 and 3000 and lo <= hi as ok,
        if(ok, lo, null) as altitude_min_m,
        if(ok, hi, null) as altitude_max_m
    from altitude_metres
),

-- harvest months
harvest_groups as (
    select
        week,
        importer,
        "row",
        regexp_extract(rule_text, harvest, 0, 'i') as whole,
        lower(regexp_extract(rule_text, harvest, 1, 'i')) as m1,
        lower(regexp_extract(rule_text, harvest, 2, 'i')) as m2
    from inputs
),

harvest_index as (
    select
        *,
        whole[1:strpos(lower(whole), m1) - 1] as lead,
        {{ month_index('m1') }} as a,
        coalesce({{ month_index('m2') }}, a) as b
    from harvest_groups
),

harvest_months as (
    select
        week,
        importer,
        "row",
        case
            when m1 = '' then null
            -- "this harvest may be smaller": a verb, unless in a month context
            when m1 = 'may' and m2 = '' and not regexp_matches(lead, '[:\-–]|\b(?:in|from)\b', 'i') then null
            else {{ month_range('a', 'b') }}
        end as harvest_months
    from harvest_index
),

-- grower
grower_parts as (
    select
        week,
        importer,
        "row",
        placeholder,
        list_filter(
            list_transform(
                -- each part without its packaging ("(GrainPro)") and surrounding whitespace
                list_transform(
                    string_split(coalesce(rule_name, ''), ' - '),
                    lambda part: {{ strip("regexp_replace(part, packaging, '', 'i')") }}
                ),
                lambda part: {
                    'part': part,
                    'kind': case
                        when regexp_matches(part, micromill, 'i') then 'micromill'
                        when regexp_matches(part, washing_station, 'i') then 'washing_station'
                        when regexp_matches(part, cooperative, 'i') then 'cooperative'
                        when regexp_matches(part, farm, 'i') then 'farm'
                    end
                }
            ),
            lambda p: p.kind is not null
        )[1] as first_grower
    from inputs
),

grower as (
    select
        week,
        importer,
        "row",
        first_grower.kind as grower_type,
        case
            when regexp_matches(first_grower.part, placeholder, 'i') or length(first_grower.part) > 80 then null
            else first_grower.part
        end as grower_name
    from grower_parts
),

-- certifications
cert_found as (
    select
        week,
        importer,
        "row",
        coalesce(rule_cert_text, '') = '' or regexp_matches(rule_cert_text, other_certs, 'i') as unknown,
        regexp_matches(rule_cert_text, organic_yes, 'i') as organic_c,
        organic_no is not null and regexp_matches(rule_cert_text, organic_no, 'i') as organic_d,
        regexp_matches(rule_cert_text, fair_trade_yes, 'i') as fair_trade_c,
        fair_trade_no is not null and regexp_matches(rule_cert_text, fair_trade_no, 'i') as fair_trade_d,
        regexp_matches(rule_cert_text, rainforest_yes, 'i') as rainforest_c,
        rainforest_no is not null and regexp_matches(rule_cert_text, rainforest_no, 'i') as rainforest_d
    from inputs
),

certifications as (
    select
        week,
        importer,
        "row",
        coalesce(unknown, true)
        or (organic_c and organic_d)
        or (fair_trade_c and fair_trade_d)
        or (rainforest_c and rainforest_d) as is_unknown,
        if(
            is_unknown,
            null,
            nullif(
                list_filter(
                    [if(organic_c, 'organic', null), if(fair_trade_c, 'fair_trade', null),
                        if(rainforest_c, 'rainforest_alliance', null)],
                    lambda x: x is not null
                ),
                []
            )
        ) as certifications,
        if(
            is_unknown,
            null,
            nullif(
                list_sort(list_filter(
                    [if(organic_d, 'organic', null), if(fair_trade_d, 'fair_trade', null),
                        if(rainforest_d, 'rainforest_alliance', null)],
                    lambda x: x is not null
                )),
                []
            )
        ) as not_certified
    from cert_found
)

select
    i.week,
    i.importer,
    i."row",
    pt.process_type,
    v.varieties,
    a.altitude_min_m,
    a.altitude_max_m,
    h.harvest_months,
    g.grower_type,
    g.grower_name,
    c.certifications,
    c.not_certified
from inputs as i
inner join process_type as pt using (week, importer, "row")
left join varieties as v using (week, importer, "row")
inner join altitude as a using (week, importer, "row")
inner join harvest_months as h using (week, importer, "row")
inner join grower as g using (week, importer, "row")
inner join certifications as c using (week, importer, "row")
