-- What the rules read from each lot, by importer (cells by their page's header):
--   rule_name: the lot's name (Cafe Imports' Name, InterAmerican's Product); grower, process and varieties read it
--   rule_origin: the page's origin, where it has one (Java is a variety except on an Indonesian lot)
--   rule_text: the free text altitude and harvest months are read from
--   rule_stated_process: the page's process column, where it has one
--   rule_cert_text: where certifications are stated
-- An importer these rules don't know yet gets no inputs, so every rule here abstains for it (stg_importer_lots' test
-- on the importer only warns, so its lots still build).
with lots as (
    select
        week,
        importer,
        "row",
        max(cell_text) filter (where column_name = 'Name') as ci_name,
        max(cell_text) filter (where column_name = 'Notes') as ci_notes,
        max(cell_text) filter (where column_name = 'Origin') as ci_origin,
        max(cell_text) filter (where column_name = 'Process') as ci_process,
        max(cell_text) filter (where column_name = 'Certification') as ci_certification,
        max(cell_text) filter (where column_name = 'Product') as ia_product,
        max(cell_text) filter (where column_name = 'Distinction') as ia_distinction
    from {{ ref('stg_importer_cells') }}
    group by all
)

select
    week,
    importer,
    "row",
    case importer when 'cafe_imports' then ci_name when 'interamerican' then ia_product end as rule_name,
    case importer when 'cafe_imports' then ci_origin end as rule_origin,
    case importer
        when 'cafe_imports' then concat_ws(' ', nullif(ci_name, ''), nullif(ci_notes, ''))
        when 'interamerican' then coalesce(ia_product, '')
    end as rule_text,
    case importer when 'cafe_imports' then ci_process end as rule_stated_process,
    case importer
        when 'cafe_imports' then ci_certification
        when 'interamerican' then concat_ws(' ', nullif(ia_product, ''), nullif(ia_distinction, ''))
    end as rule_cert_text
from lots
