-- Royal Coffee's labelled values: every "Label: value" a tag or a body line gives (int_royal_rules reads them), in
-- order (the tags first, then the body), each value once per label. A line can hold several labels after a full stop
-- or semicolon ("... lot. Certifications: Organic"). A value loses a trailing full stop, semicolon, comma or colon, so
-- "Region: Huila." and "Region: Huila" agree. The labels and their patterns are importer_rules.ROYAL_LABELS.
with vocab as (
    select * from {{ ref('stg_importer_vocab') }}
),

part_break as (
    select value as part_break from vocab where kind = 'pattern' and term = 'royal_part_break'
),

texts as (
    select week, importer, "row", column_name, cell_text
    from {{ ref('stg_importer_cells') }}
    where importer = 'royal_coffee' and column_name in ('tags', 'body_text')
),

lines as (
    select
        t.week,
        t.importer,
        t."row",
        if(t.column_name = 'tags', 1, 2) as source_order,
        l.line,
        l.line_no
    from texts as t,
        unnest({{ lines('t.cell_text') }}) with ordinality as l (line, line_no)
),

parts as (
    select
        l.week,
        l.importer,
        l."row",
        l.source_order,
        l.line_no,
        {{ strip('p.part') }} as part,
        p.part_no
    from lines as l
    cross join part_break as b,
        unnest(string_split(regexp_replace(l.line, b.part_break, '\1' || chr(31) || '\2', 'g'), chr(31)))
            with ordinality as p (part, part_no)
),

values_found as (
    select
        p.week,
        p.importer,
        p."row",
        v.term as label,
        regexp_replace(regexp_extract(p.part, v.value, 1, 'i'), '[ .;,:]+$', '') as label_value,
        [p.source_order, p.line_no, p.part_no] as position
    from parts as p
    inner join vocab as v on v.kind = 'royal_label' and regexp_matches(p.part, v.value, 'i')
)

select week, importer, "row", label, label_value, position
from values_found
where label_value != ''
qualify row_number() over (partition by week, importer, "row", label, label_value order by position) = 1
