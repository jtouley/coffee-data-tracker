-- Where a Royal lot has exactly one kind of process tag ("washed", "natural", a honey or wet-hulled tag), its
-- process_type is that kind, anaerobic (a fermentation the tags don't name), or null (decaf, or a title naming another
-- process: abstain). Returns a row, and fails, for each lot that contradicts its tag.
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
),

patterns as (
    select
        max(value) filter (where kind = 'pattern' and term = 'royal_process_tag') as process_tag_re,
        max(value) filter (where kind = 'pattern' and term = 'royal_honey') as honey,
        max(value) filter (where kind = 'process' and term = 'anaerobic') as anaerobic,
        max(value) filter (where kind = 'process' and term = 'natural') as natural_re,
        max(value) filter (where kind = 'process' and term = 'washed') as washed_re
    from vocab
),

tags as (
    select c.week, c.importer, c."row", t.tag, p.*
    from {{ ref('stg_importer_cells') }} as c
    cross join patterns as p,
        unnest({{ lines('c.cell_text') }}) as t (tag)
    where c.importer = 'royal_coffee' and c.column_name = 'tags' and regexp_matches(t.tag, p.process_tag_re, 'i')
),

tag_kinds as (
    select
        week,
        importer,
        "row",
        list(distinct {{ process_kind('tag', 'anaerobic', 'honey', 'natural_re', 'washed_re') }})
            filter (where {{ process_kind('tag', 'anaerobic', 'honey', 'natural_re', 'washed_re') }} is not null)
            as kinds
    from tags
    group by all
)

select o.week, o.importer, o."row", o.process_type, t.kinds
from {{ ref('importer_offers') }} as o
inner join tag_kinds as t using (week, importer, "row")
where len(t.kinds) = 1 and o.process_type is not null and o.process_type not in (t.kinds[1], 'anaerobic')
