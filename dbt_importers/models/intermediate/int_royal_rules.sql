-- Royal Coffee's values, from each product's title, tags and body text and its labelled values (int_royal_labels).
-- Built only from invented products (tests/fixtures/importers/royal_synthetic_products.json). Each abstains (null)
-- when unsure: no label, two values that disagree, a negation, a unit or wording it doesn't know.
--   lot_id: the lot number in the title ("Lot 70412", "Lot #70588"); null for none, two, or a year ("Harvest Lot
--     2025").
--   warehouse: a title part naming one ("Harborview Warehouse") or a "Warehouse:" label; null when two disagree or the
--     part is only a preposition or a negation ("In Warehouse", "Not In Warehouse").
--   origin: an "Origin:" label's country (an alias read as its country: "Origin: Sumatra" is Indonesia), else the one
--     country the title names; of the island and state names only those that mean one place in a coffee title
--     (importer_rules.ROYAL_TITLE_PLACES_OK): "Java" is also a variety and "Flores" a farm name.
--   region: a "Region:" label, as written. process: a "Process:" label, as written.
--   process_type: washed, natural, honey or anaerobic. Decaf anywhere abstains. Anaerobic (or carbonic) in a label,
--     the title or a process tag wins. Otherwise "Process:" labels decide when there are any (all must agree); else
--     the title and the tags that are a process word and nothing else ("Natural"), which must name one between them.
--   varieties: from "Variety:" labels, in the catalog's spelling; null when a label names one the catalog doesn't know.
--   altitude_min_m, altitude_max_m: an "Altitude:" label that is a number or range in metres and nothing else.
--   harvest_months: a "Harvest:" label that is one month or a range ("October - January"), optionally with its year.
--   certifications: claimed by "Certification:" labels, tags that are a certification and nothing else, and the
--     title. Null when nothing claims one; when any of them names a program we don't model or negates a modeled one;
--     and for a bare "FTO" unless fair trade and organic are both claimed outright. The store marks none absent.
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
),

patterns as (
    select
        max(value) filter (where kind = 'pattern' and term = 'royal_title_parts') as title_parts,
        max(value) filter (where kind = 'pattern' and term = 'royal_lot') as lot_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_year') as year_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_warehouse') as warehouse_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_not_a_name') as not_a_name,
        max(value) filter (where kind = 'pattern' and term = 'royal_negated') as negated,
        max(value) filter (where kind = 'pattern' and term = 'royal_decaf') as decaf,
        max(value) filter (where kind = 'pattern' and term = 'royal_honey') as honey,
        max(value) filter (where kind = 'process' and term = 'anaerobic') as anaerobic,
        max(value) filter (where kind = 'process' and term = 'honey') as catalog_honey,
        max(value) filter (where kind = 'process' and term = 'natural') as natural_re,
        max(value) filter (where kind = 'process' and term = 'washed') as washed_re,
        max(value) filter (where kind = 'pattern' and term = 'varietal') as varietal,
        max(value) filter (where kind = 'pattern' and term = 'royal_variety_filler') as variety_filler,
        max(value) filter (where kind = 'pattern' and term = 'royal_altitude') as altitude_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_harvest') as harvest_re,
        max(value) filter (where kind = 'pattern' and term = 'other_certs') as other_certs,
        max(value) filter (where kind = 'pattern' and term = 'royal_cert_negated') as cert_negated,
        max(value) filter (where kind = 'pattern' and term = 'royal_fto') as fto_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_cert_filler') as cert_filler,
        max(value) filter (where kind = 'royal_certification' and term = 'organic') as organic_re,
        max(value) filter (where kind = 'royal_certification' and term = 'fair_trade') as fair_trade_re,
        max(value) filter (where kind = 'royal_certification' and term = 'rainforest_alliance') as rainforest_re
    from vocab
),

cells as (
    select
        week,
        importer,
        "row",
        coalesce(max(cell_text) filter (where column_name = 'title'), '') as title,
        max(cell_text) filter (where column_name = 'tags') as tags,
        max(cell_text) filter (where column_name = 'product_type') as product_type,
        max(cell_text) filter (where column_name = 'body_text') as body_text
    from {{ ref('stg_importer_cells') }}
    where importer = 'royal_coffee'
    group by all
),

labels as (
    select
        week,
        importer,
        "row",
        list(label_value order by position) filter (where label = 'process') as process_labels,
        list(label_value order by position) filter (where label = 'region') as region_labels,
        list(label_value order by position) filter (where label = 'origin') as origin_labels,
        list(label_value order by position) filter (where label = 'variety') as variety_labels,
        list(label_value order by position) filter (where label = 'altitude') as altitude_labels,
        list(label_value order by position) filter (where label = 'harvest') as harvest_labels,
        list(label_value order by position) filter (where label = 'certification') as certification_labels,
        list(label_value order by position) filter (where label = 'warehouse') as warehouse_labels
    from {{ ref('int_royal_labels') }}
    group by all
),

lots as (
    select
        c.*,
        coalesce(l.process_labels, []) as process_labels,
        coalesce(l.region_labels, []) as region_labels,
        coalesce(l.origin_labels, []) as origin_labels,
        coalesce(l.variety_labels, []) as variety_labels,
        coalesce(l.altitude_labels, []) as altitude_labels,
        coalesce(l.harvest_labels, []) as harvest_labels,
        coalesce(l.certification_labels, []) as certification_labels,
        coalesce(l.warehouse_labels, []) as warehouse_labels,
        -- the tags that aren't a label ("Natural", "Organic")
        list_filter({{ lines('c.tags') }}, lambda t: not contains(t, ':')) as plain_tags,
        p.*
    from cells as c
    left join labels as l using (week, importer, "row")
    cross join patterns as p
),

-- lot number and warehouse, from the title
title_parts as (
    select
        week,
        importer,
        "row",
        list_transform(
            list_filter(
                list_transform(regexp_split_to_array(title, title_parts), lambda part: {{ strip('part') }}),
                lambda part: regexp_matches(part, warehouse_re, 'i')
            ),
            lambda part: {
                'part': part,
                'name': {{ strip("coalesce(nullif(regexp_extract(part, warehouse_re, 1, 'i'), ''), "
                    ~ "regexp_extract(part, warehouse_re, 2, 'i'))") }}
            }
        ) as warehouse_parts
    from lots
),

stated as (
    select
        l.week,
        l.importer,
        l."row",
        case
            when list_bool_or(list_transform(regexp_extract_all(l.title, l.lot_re, 1, 'i'),
                lambda v: regexp_matches(v, l.year_re))) then null
            else {{ one_value("regexp_extract_all(l.title, l.lot_re, 1, 'i')") }}
        end as lot_id,
        case
            when list_bool_or(list_transform(t.warehouse_parts,
                lambda w: regexp_matches(w.name, l.not_a_name, 'i') or regexp_matches(w.part, l.negated, 'i')))
                then null
            else {{ one_value("list_concat(list_transform(t.warehouse_parts, lambda w: w.name), l.warehouse_labels)") }}
        end as warehouse,
        {{ one_value('l.region_labels') }} as region,
        {{ one_value('l.process_labels') }} as process
    from lots as l
    inner join title_parts as t using (week, importer, "row")
),

-- origin: a label's country, else the title's one country
origin_labelled as (
    select
        l.week,
        l.importer,
        l."row",
        list_distinct(list_transform(l.origin_labels, lambda v: coalesce(c.country_of[lower({{ strip('v') }})], '?')))
            as names
    from lots as l
    cross join (
        select map_from_entries(list({'key': term, 'value': value})) as country_of
        from vocab where kind = 'country'
    ) as c
),

origin_in_title as (
    select l.week, l.importer, l."row", count(distinct n.value) as found, any_value(n.value) as country
    from lots as l
    inner join vocab as n
        on n.kind = 'country_name'
        and (n.value2 is null or lower(n.term) in (select term from vocab where kind = 'royal_title_place_ok'))
        and regexp_matches(l.title, '\b' || regexp_escape(n.term) || '\b', 'i')
    group by all
),

origin as (
    select
        l.week,
        l.importer,
        l."row",
        case
            when len(l.origin_labels) > 0 then
                if(len(o.names) = 1 and o.names[1] in (select term from vocab where kind = 'country_name'), o.names[1], null)
            when t.found = 1 then t.country
        end as origin
    from lots as l
    inner join origin_labelled as o using (week, importer, "row")
    left join origin_in_title as t using (week, importer, "row")
),

-- process type
process_inputs as (
    select
        *,
        list_filter(
            plain_tags,
            lambda t: {{ process_kind('t', 'anaerobic', 'honey', 'natural_re', 'washed_re') }} is not null
            and len(string_split_regex({{ strip('t') }}, '\s+')) <= 3
        ) as process_tags
    from lots
),

process_kinds as (
    select
        *,
        -- each label's process, '?' where one names none or several
        list_distinct(list_transform(process_labels, lambda v: coalesce(
            {{ process_kind('v', 'anaerobic', 'honey', 'natural_re', 'washed_re') }}, '?'
        ))) as label_kinds,
        -- the processes the title and the process tags name between them
        list_distinct(list_filter(
            list_transform(
                list_concat([title], process_tags),
                lambda v: {{ process_kind('v', 'anaerobic', 'honey', 'natural_re', 'washed_re') }}
            ),
            lambda k: k is not null
        )) as free_kinds
    from process_inputs
),

process_type as (
    select
        week,
        importer,
        "row",
        case
            when regexp_matches(concat_ws(' ', title, tags, product_type, body_text), decaf, 'i') then null
            when regexp_matches(
                array_to_string(list_concat([title], process_tags, process_labels), ' '), anaerobic, 'i'
            ) then 'anaerobic'
            -- every label must name the same one process
            when len(process_labels) > 0 then if(len(label_kinds) = 1 and label_kinds[1] != '?', label_kinds[1], null)
            -- the title names two processes: unclear, whatever a tag says
            when {{ process_kind('title', 'anaerobic', 'honey', 'natural_re', 'washed_re') }} is null
                and (regexp_matches(title, catalog_honey, 'i') or regexp_matches(title, natural_re, 'i')
                    or regexp_matches(title, washed_re, 'i')) then null
            when len(free_kinds) = 1 then free_kinds[1]
        end as process_type
    from process_kinds
),

-- varieties: every name in each label must be one the catalog knows
variety_values as (
    select
        l.week,
        l.importer,
        l."row",
        v.label_value,
        regexp_extract_all(v.label_value, l.varietal, 1, 'i') as matched,
        regexp_replace(regexp_replace(v.label_value, l.varietal, '\2', 'gi'), l.variety_filler, '', 'gi') as leftover
    from lots as l,
        unnest(l.variety_labels) as v (label_value)
),

variety_names as (
    select
        v.week,
        v.importer,
        v."row",
        bool_or(len(v.matched) = 0 or v.leftover != '') as unknown_name,
        list_sort(list_distinct(flatten(list(list_transform(v.matched, lambda m: coalesce(
            n.names[regexp_replace(lower(m), '[\s-]', '', 'g')], m
        )))))) as varieties
    from variety_values as v
    cross join (
        select map_from_entries(list({'key': term, 'value': value})) as names from vocab where kind = 'varietal'
    ) as n
    group by all
),

-- altitude and harvest months, from one label each
measures as (
    select
        week,
        importer,
        "row",
        altitude_re,
        harvest_re,
        coalesce({{ one_value('altitude_labels') }}, '') as altitude_label,
        coalesce({{ one_value('harvest_labels') }}, '') as harvest_label
    from lots
),

measured as (
    select
        week,
        importer,
        "row",
        try_cast(replace(regexp_extract(altitude_label, altitude_re, 1, 'i'), ',', '') as integer) as lo,
        coalesce(
            try_cast(replace(regexp_extract(altitude_label, altitude_re, 2, 'i'), ',', '') as integer), lo
        ) as hi,
        {{ month_index("regexp_extract(harvest_label, harvest_re, 1, 'i')") }} as m1,
        coalesce({{ month_index("regexp_extract(harvest_label, harvest_re, 2, 'i')") }}, m1) as m2
    from measures
),

-- certifications
cert_texts as (
    select
        *,
        list_concat(certification_labels, plain_tags, [title]) as cert_sources,
        list_transform(
            certification_labels,
            lambda v: {
                'organic': regexp_matches(v, organic_re, 'i'),
                'fair_trade': regexp_matches(v, fair_trade_re, 'i'),
                'rainforest_alliance': regexp_matches(v, rainforest_re, 'i'),
                'leftover': regexp_replace(
                    regexp_replace(regexp_replace(regexp_replace(regexp_replace(
                        v, fto_re, '', 'g'), organic_re, '', 'gi'), fair_trade_re, '', 'gi'), rainforest_re, '', 'gi'),
                    cert_filler, '', 'gi'
                )
            }
        ) as label_claims
    from lots
),

cert_claims as (
    select
        week,
        importer,
        "row",
        list_bool_or(list_transform(cert_sources,
            lambda t: regexp_matches(t, other_certs, 'i') or regexp_matches(t, cert_negated, 'i'))) as unclear,
        list_bool_or(list_transform(cert_sources, lambda t: regexp_matches(t, fto_re))) as fto,
        list_bool_or(list_transform(label_claims, lambda c: c.leftover != '')) as unknown_label,
        list_bool_or(list_transform(label_claims, lambda c: c.organic))
        or list_bool_or(list_transform(plain_tags, lambda t: regexp_full_match(t, '\s*(?:' || organic_re || ')\s*', 'i')))
        or regexp_matches(title, organic_re, 'i') as organic,
        list_bool_or(list_transform(label_claims, lambda c: c.fair_trade))
        or list_bool_or(list_transform(plain_tags, lambda t: regexp_full_match(t, '\s*(?:' || fair_trade_re || ')\s*', 'i')))
        or regexp_matches(title, fair_trade_re, 'i') as fair_trade,
        list_bool_or(list_transform(label_claims, lambda c: c.rainforest_alliance))
        or list_bool_or(list_transform(plain_tags, lambda t: regexp_full_match(t, '\s*(?:' || rainforest_re || ')\s*', 'i')))
        or regexp_matches(title, rainforest_re, 'i') as rainforest_alliance
    from cert_texts
),

certifications as (
    select
        week,
        importer,
        "row",
        case
            when coalesce(unclear, false) or coalesce(unknown_label, false) then null
            when coalesce(fto, false) and not (coalesce(organic, false) and coalesce(fair_trade, false)) then null
            else nullif(list_filter(
                [if(organic, 'organic', null), if(fair_trade, 'fair_trade', null),
                    if(rainforest_alliance, 'rainforest_alliance', null)],
                lambda x: x is not null
            ), [])
        end as certifications
    from cert_claims
)

select
    s.week,
    s.importer,
    s."row",
    s.lot_id,
    s.warehouse,
    o.origin,
    s.region,
    s.process,
    pt.process_type,
    if(coalesce(vn.unknown_name, true), null, vn.varieties) as varieties,
    if(m.lo between 300 and 3000 and m.hi between 300 and 3000 and m.lo <= m.hi, m.lo, null) as altitude_min_m,
    if(m.lo between 300 and 3000 and m.hi between 300 and 3000 and m.lo <= m.hi, m.hi, null) as altitude_max_m,
    if(m.m1 is null, null, {{ month_range('m.m1', 'm.m2') }}) as harvest_months,
    c.certifications
from stated as s
inner join origin as o using (week, importer, "row")
inner join process_type as pt using (week, importer, "row")
left join variety_names as vn using (week, importer, "row")
inner join measured as m using (week, importer, "row")
inner join certifications as c using (week, importer, "row")
