-- What an InterAmerican product name states, where its table has no column for it: "Brazil Oberon 17/18 Natural",
-- "Ethiopia Yirgacheffe G2 Washed", "Guatemala Huehuetenango ASDECAFE SHB EP Organic". The country leads, then any of
-- a region, a mark or farm, a grade or screen, a process and certifications (int_importer_rules reads those). Each
-- value abstains (null) when the name doesn't settle it.
--   origin: the catalog's country (origins.COUNTRIES and its aliases) when the name leads with a country or alias and
--     names no other one; null for a blend, or a name that doesn't start with one (a misspelling, "Guatemalal"). Of
--     the aliases, only those that mean one place in a name count (importer_rules.TITLE_PLACES_OK):
--     "Java" is also a variety and "Flores" a farm name, so "Nicaragua Java Natural" is Nicaragua and "Java G1" none.
--   region: as the name writes it, one of the catalog's one-country regions (origins.REGIONS) of the lot's country,
--     else an alias of that country that isn't the name's leading word ("Indonesia Sumatra"); null when the name
--     gives none, several, one of another country, or a farm named after one ("Finca Santa Barbara").
--   process: the catalog's process words the name has, as it writes them, in order ("Natural", "Honey Natural");
--     "Decaf" for a decaffeinated lot, as Cafe Imports' process column has it; null when it names none.
--   grade: the grade and preparation words it states, in order: "G1", "UG", "SHB", "EP", "EP10", "TP", "AA",
--     "Supremo", "Peaberry".
--   screen: a screen size range, "17/18", when the name has exactly one; never two adjacent sizes from 18 up ("19/20"
--     reads as a crop year).
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
),

patterns as (
    select
        max(value) filter (where kind = 'pattern' and term = 'country_lead') as country_lead,
        max(value) filter (where kind = 'pattern' and term = 'country') as country,
        max(value) filter (where kind = 'pattern' and term = 'region') as region,
        max(value) filter (where kind = 'pattern' and term = 'blend') as blend,
        max(value) filter (where kind = 'pattern' and term = 'farm_word') as farm_word,
        max(value) filter (where kind = 'pattern' and term = 'decaf') as decaf,
        max(value) filter (where kind = 'pattern' and term = 'honey') as honey,
        max(value) filter (where kind = 'process' and term = 'anaerobic') as anaerobic,
        max(value) filter (where kind = 'process' and term = 'natural') as natural_re,
        max(value) filter (where kind = 'process' and term = 'washed') as washed,
        max(value) filter (where kind = 'pattern' and term = 'grade') as grade,
        max(value) filter (where kind = 'pattern' and term = 'screen') as screen
    from vocab
),

countries as (
    select term, value as country from vocab where kind = 'country'
),

regions as (
    select term, value as country from vocab where kind = 'region'
),

not_a_place as (
    select term from vocab
    where kind = 'country_name' and value2 = 'alias'
        and lower(term) not in (select term from vocab where kind = 'title_place_ok')
),

names as (
    select i.week, i.importer, i."row", i.rule_name as product, p.*
    from {{ ref('int_importer_inputs') }} as i
    cross join patterns as p
    where i.importer = 'interamerican' and coalesce(i.rule_name, '') != ''
),

-- origin: every country or alias the name names, in order
country_mentions as (
    select
        n.week,
        n.importer,
        n."row",
        m.mention,
        m.position,
        c.country,
        -- an alias that is also a variety, a farm name or two countries never counts as an origin
        lower(m.mention) not in (select lower(term) from not_a_place) as counts
    from names as n,
        unnest(regexp_extract_all(n.product, n.country, 1, 'i')) with ordinality as m (mention, position)
    inner join countries as c on c.term = lower(m.mention)
),

origin as (
    select
        n.week,
        n.importer,
        n."row",
        case
            when regexp_matches(n.product, n.blend, 'i') then null
            when not regexp_matches(n.product, n.country_lead, 'i') then null
            when not bool_or(cm.position = 1 and cm.counts) then null  -- it leads with "Java", not a country
            when count(distinct cm.country) filter (where cm.counts) = 1 then any_value(cm.country) filter (where cm.counts)
        end as origin
    from names as n
    left join country_mentions as cm using (week, importer, "row")
    group by n.week, n.importer, n."row", n.product, n.blend, n.country_lead
),

-- region: a one-country region of the lot's country, else one of its aliases after the leading word
region_mentions as (
    select n.week, n.importer, n."row", m.mention, m.position, r.country = o.origin as of_origin
    from names as n,
        unnest(regexp_extract_all(n.product, n.region, 1, 'i')) with ordinality as m (mention, position)
    inner join regions as r on r.term = lower(m.mention)
    inner join origin as o on o.week = n.week and o.importer = n.importer and o."row" = n."row"
),

-- a region of another country in the name ("Colombia Huila Tarrazu"): a mark or a mistake, so no region at all
other_country_region as (
    select distinct week, importer, "row" from region_mentions where not coalesce(of_origin, false)
),

candidates as (
    select week, importer, "row", mention, position, 1 as rank
    from region_mentions
    where of_origin
    union all
    select cm.week, cm.importer, cm."row", cm.mention, cm.position, 2 as rank
    from country_mentions as cm
    inner join origin as o using (week, importer, "row")
    where cm.country = o.origin and cm.position > 1 and lower(cm.mention) != lower(o.origin)
),

best_rank as (
    select week, importer, "row", min(rank) as rank from candidates group by all
),

region as (
    select
        c.week,
        c.importer,
        c."row",
        if(count(distinct lower(c.mention)) = 1, arg_min(c.mention, c.position), null) as region
    from candidates as c
    inner join best_rank using (week, importer, "row", rank)
    group by all
),

-- process, grade and screen
stated as (
    select
        week,
        importer,
        "row",
        product,
        farm_word,
        case
            when regexp_matches(product, decaf, 'i') then 'Decaf'
            else nullif(array_to_string(regexp_extract_all(
                product, '(?:' || anaerobic || ')|(?:' || honey || ')|(?:' || natural_re || ')|(?:' || washed || ')', 0, 'i'
            ), ' '), '')
        end as process,
        list_transform(
            regexp_extract_all(product, grade, 1),
            lambda g: case
                when regexp_matches(g, '^(?i:grade) ') then 'G' || right(g, 1)
                when regexp_matches(g, '^EP ?10$') then 'EP10'
                when regexp_matches(g, '^[A-Z0-9]+$') then g
                else upper(g[1]) || lower(g[2:])
            end
        ) as grades,
        regexp_extract_all(product, screen, 0) as numbers
    from names
)

select
    s.week,
    s.importer,
    s."row",
    o.origin,
    case
        when x."row" is not null then null
        when regexp_matches(s.product, s.farm_word || regexp_escape(r.region), 'i') then null
        else r.region
    end as region,
    s.process,
    nullif(list_filter(s.grades, lambda g, i: list_position(s.grades, g) = i), []) as grade,
    case
        when len(s.numbers) = 1
            and regexp_matches(s.numbers[1], '^\d{1,2}/\d{1,2}$')
            and split_part(s.numbers[1], '/', 1)::int between 12 and 20
            and split_part(s.numbers[1], '/', 2)::int between 12 and 20
            and split_part(s.numbers[1], '/', 2)::int - split_part(s.numbers[1], '/', 1)::int between 1 and 4
            -- "19/20", "18/19": a crop year as likely as a screen
            and not (split_part(s.numbers[1], '/', 1)::int >= 18
                and split_part(s.numbers[1], '/', 2)::int - split_part(s.numbers[1], '/', 1)::int = 1)
            then s.numbers[1]
    end as screen
from stated as s
inner join origin as o using (week, importer, "row")
left join region as r using (week, importer, "row")
left join other_country_region as x using (week, importer, "row")
