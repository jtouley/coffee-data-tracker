-- Royal Coffee's values, from each product's title, tags, product type and variants. Royal's title is " - "-separated
-- parts: the coffee's name, leading with its origin, then any of a contract number, Royal's lot ID, the bags, a
-- shipment or arrival date, and last the warehouse (importer_rules' Royal section shows the shapes, invented). Its
-- body is prose with no "Label: value" lines, so it isn't read: region, varieties, altitude and harvest months stay
-- null, left for Claude. Each value abstains (null) when the text doesn't settle it.
--   lot_id: the one title part that is Royal's five-digit lot ID ("12345", a split lot's "12345-2"); null for none or
--     two that differ. Never a producer's "Lot 3" or a starred contract number.
--   warehouse: the last title part when it is a warehouse code in capitals, on a title with a lot ID part, without its
--     "SPOT" ("SPOT EXAMPLEWHSE" is EXAMPLEWHSE).
--   status: 'spot' when that part says SPOT (the lot is in the warehouse now), the other importers' word for it; null
--     otherwise: a shipment month doesn't say whether the lot has left origin.
--   est_arrival: an "ETA: Nov 30, 2026" or "January 2027 Delivery" part, as written, when it parses as a date or
--     month; "... Shipment" and "Intend Ship:" are when a lot leaves origin, so they give none.
--   bag_size: the title part naming the packaging, as written ("69.0 kg GrainPro Bags").
--   bag_kg: the one weight the title states ("69.0 kg", "22 Lb" in kg), else the one variant's grams in kg.
--   available: whether any variant is available (Shopify's variants.available): the lot is offered now.
--   origin: the first place the title's name part names, as the catalog's country, longest name first ("Papua New
--     Guinea", never "Guinea"); island names as the catalog reads them (Java, Bali, Flores, Sumatra: Indonesia) and
--     Royal's TIMOR LESTE, bare TIMOR and KOMODO (importer_rules.ROYAL_PLACES). Null for a blend that names two
--     countries, a first place that names no one country ("New Guinea", "Congo"), or none.
--   process: the tag that is a process and nothing else ("washed", "wet-hulled"), as written.
--   process_type: washed, natural, honey or anaerobic. Decaf in the title, tags or product type abstains (the body's
--     prose can mention sugar cane or water on any lot). Anaerobic (or carbonic) in the title or a process tag wins;
--     otherwise the title and the process tags must name one between them.
--   certifications: claimed by the title or a tag that is a certification and nothing else; Royal's FT, FT-FLO,
--     FT-USA and FT-FLO/USA are fair trade. Null when nothing claims one, when anything names a program we don't
--     model or negates a modeled one, and for a bare "FTO" unless fair trade and organic are both claimed outright.
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
),

patterns as (
    select
        max(value) filter (where kind = 'pattern' and term = 'royal_title_parts') as title_parts,
        max(value) filter (where kind = 'pattern' and term = 'royal_lot_id') as lot_id_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_warehouse') as warehouse_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_arrival') as arrival_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_bag_weight') as bag_weight_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_bag_part') as bag_part_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_process_tag') as process_tag_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_origin_lead') as origin_lead,
        max(value) filter (where kind = 'pattern' and term = 'blend') as blend_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_decaf') as decaf,
        max(value) filter (where kind = 'pattern' and term = 'royal_honey') as honey,
        max(value) filter (where kind = 'process' and term = 'anaerobic') as anaerobic,
        max(value) filter (where kind = 'process' and term = 'honey') as catalog_honey,
        max(value) filter (where kind = 'process' and term = 'natural') as natural_re,
        max(value) filter (where kind = 'process' and term = 'washed') as washed_re,
        max(value) filter (where kind = 'pattern' and term = 'other_certs') as other_certs,
        max(value) filter (where kind = 'pattern' and term = 'royal_cert_negated') as cert_negated,
        max(value) filter (where kind = 'pattern' and term = 'royal_fto') as fto_re,
        max(value) filter (where kind = 'royal_certification' and term = 'organic') as organic_re,
        max(value) filter (where kind = 'royal_certification' and term = 'fair_trade') as fair_trade_re,
        max(value) filter (where kind = 'royal_certification' and term = 'rainforest_alliance') as rainforest_re
    from vocab
),

-- a place's country: the catalog's countries and aliases (lowercase), and Royal's own spellings
places as (
    select
        map_from_entries(list({'key': term, 'value': value}) filter (where kind in ('country', 'royal_place')))
            as country_of,
        list(term) filter (where kind = 'royal_ambiguous_place') as ambiguous
    from vocab
    where kind in ('country', 'royal_place', 'royal_ambiguous_place')
),

catalog as (
    select list(value) as countries from vocab where kind = 'catalog_country'
),

cells as (
    select
        week,
        importer,
        "row",
        coalesce(max(cell_text) filter (where column_name = 'title'), '') as title,
        max(cell_text) filter (where column_name = 'tags') as tags,
        max(cell_text) filter (where column_name = 'product_type') as product_type,
        max(cell_text) filter (where column_name = 'variants') as variants
    from {{ ref('stg_importer_cells') }}
    where importer = 'royal_coffee'
    group by all
),

lots as (
    select
        c.*,
        list_transform(regexp_split_to_array({{ strip('c.title') }}, p.title_parts), lambda part: {{ strip('part') }})
            as parts,
        -- the tags that are one word or name, not a label ("natural", "Organic")
        list_filter({{ lines('c.tags') }}, lambda t: not contains(t, ':')) as plain_tags,
        -- Shopify's variants, as stored: [{available, grams, ...}]; null when they aren't a JSON list
        try(from_json(c.variants, '[{"available": "BOOLEAN", "grams": "DOUBLE"}]')) as variant_list,
        p.*
    from cells as c
    cross join patterns as p
),

-- lot ID, warehouse, status, arrival and bags, from the title's parts
title_fields as (
    select
        week,
        importer,
        "row",
        list_distinct(list_filter(parts, lambda part: regexp_matches(part, lot_id_re))) as lot_ids,
        if(len(parts) >= 2 and regexp_matches(parts[-1], warehouse_re), parts[-1], null) as warehouse_part,
        list_filter(
            list_transform(
                list_filter(parts[2:], lambda part: regexp_matches(part, arrival_re, 'i')),
                lambda part: regexp_replace(
                    coalesce(nullif(regexp_extract(part, arrival_re, 1, 'i'), ''), regexp_extract(part, arrival_re, 2, 'i')),
                    '{{ ws() }}+', ' ', 'g'
                )
            ),
            -- only a date or a month that parses ("Nov 30, 2026", "January 2027")
            lambda d: try_strptime(replace(d, '.', ''), ['%b %d, %Y', '%B %d, %Y', '%b %Y', '%B %Y']) is not null
        ) as arrivals,
        regexp_extract_all(title, bag_weight_re, 0, 'i') as weights,
        list_filter(parts[2:], lambda part: regexp_matches(part, bag_part_re, 'i')) as bag_parts,
        warehouse_re,
        bag_weight_re
    from lots
),

stated as (
    select
        f.week,
        f.importer,
        f."row",
        if(len(f.lot_ids) = 1, f.lot_ids[1], null) as lot_id,
        if(len(f.lot_ids) >= 1, regexp_extract(f.warehouse_part, f.warehouse_re, 2), null) as warehouse,
        if(len(f.lot_ids) >= 1 and regexp_extract(f.warehouse_part, f.warehouse_re, 1) != '', 'spot', null) as status,
        if(len(list_distinct(f.arrivals)) = 1, f.arrivals[1], null) as est_arrival,
        if(len(f.bag_parts) = 1, f.bag_parts[1], null) as bag_size,
        case
            when len(f.weights) = 1 then
                case lower(regexp_extract(f.weights[1], f.bag_weight_re, 2, 'i'))
                    when 'kg' then try_cast(regexp_extract(f.weights[1], f.bag_weight_re, 1, 'i') as double)
                    when 'lb' then round(
                        try_cast(regexp_extract(f.weights[1], f.bag_weight_re, 1, 'i') as double) * 0.45359237, 2
                    )
                end
            when len(f.weights) = 0 and len(l.variant_list) = 1 and l.variant_list[1].grams > 0
                then round(l.variant_list[1].grams / 1000, 1)
        end as bag_kg,
        list_bool_or(list_transform(l.variant_list, lambda v: v.available)) as available,
        {{ one_value("list_filter(l.plain_tags, lambda t: regexp_matches(" ~ strip('t') ~ ", l.process_tag_re, 'i'))") }}
            as process
    from title_fields as f
    inner join lots as l using (week, importer, "row")
),

-- origin: the first place the name part names
origin as (
    select
        l.week,
        l.importer,
        l."row",
        list_transform(
            regexp_extract_all(coalesce(l.parts[1], ''), l.origin_lead, 1, 'i'), lambda v: lower(v)
        ) as places,
        regexp_matches(coalesce(l.parts[1], ''), l.blend_re, 'i') as blend
    from lots as l
),

origin_country as (
    select
        o.week,
        o.importer,
        o."row",
        case
            when len(o.places) = 0 or list_contains(p.ambiguous, o.places[1]) then null
            -- a blend of one country's lots is that country's; of two countries', none
            when o.blend and len(list_distinct(list_transform(o.places, lambda v: coalesce(p.country_of[v], v)))) != 1
                then null
            when list_contains(c.countries, p.country_of[o.places[1]]) then p.country_of[o.places[1]]
        end as origin
    from origin as o
    cross join places as p
    cross join catalog as c
),

-- process type
process_inputs as (
    select
        *,
        list_filter(
            plain_tags,
            lambda t: {{ process_kind('t', 'anaerobic', 'honey', 'natural_re', 'washed_re') }} is not null
            and len(string_split_regex({{ strip('t') }}, '{{ ws() }}+')) <= 3
        ) as process_tags
    from lots
),

process_kinds as (
    select
        *,
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
            -- the title, tags and product type say decaf; the body isn't read
            when regexp_matches(concat_ws(' ', title, tags, product_type), decaf, 'i') then null
            when regexp_matches(array_to_string(list_concat([title], process_tags), ' '), anaerobic, 'i') then 'anaerobic'
            -- the title names two processes: unclear, whatever a tag says
            when {{ process_kind('title', 'anaerobic', 'honey', 'natural_re', 'washed_re') }} is null
                and (regexp_matches(title, catalog_honey, 'i') or regexp_matches(title, natural_re, 'i')
                    or regexp_matches(title, washed_re, 'i')) then null
            when len(free_kinds) = 1 then free_kinds[1]
        end as process_type
    from process_kinds
),

-- certifications
cert_claims as (
    select
        week,
        importer,
        "row",
        list_bool_or(list_transform(list_concat(plain_tags, [title]),
            lambda t: regexp_matches(t, other_certs, 'i') or regexp_matches(t, cert_negated, 'i'))) as unclear,
        list_bool_or(list_transform(list_concat(plain_tags, [title]), lambda t: regexp_matches(t, fto_re))) as fto,
        list_bool_or(list_transform(plain_tags, lambda t: regexp_full_match(t, '{{ ws() }}*(?:' || organic_re || '){{ ws() }}*', 'i')))
        or regexp_matches(title, organic_re, 'i') as organic,
        list_bool_or(list_transform(plain_tags, lambda t: regexp_full_match(t, '{{ ws() }}*(?:' || fair_trade_re || '){{ ws() }}*', 'i')))
        or regexp_matches(title, fair_trade_re, 'i') as fair_trade,
        list_bool_or(list_transform(plain_tags, lambda t: regexp_full_match(t, '{{ ws() }}*(?:' || rainforest_re || '){{ ws() }}*', 'i')))
        or regexp_matches(title, rainforest_re, 'i') as rainforest_alliance
    from lots
),

certifications as (
    select
        week,
        importer,
        "row",
        case
            when coalesce(unclear, false) then null
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
    s.status,
    s.est_arrival,
    s.bag_size,
    s.bag_kg,
    s.available,
    o.origin,
    s.process,
    pt.process_type,
    c.certifications
from stated as s
inner join origin_country as o using (week, importer, "row")
inner join process_type as pt using (week, importer, "row")
inner join certifications as c using (week, importer, "row")
