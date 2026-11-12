-- One row per cell of each lot: the column (the page's header) and its text, verbatim (null when empty).
with cells as (
    select week, importer, "row", unnest(cells) as cell, generate_subscripts(cells, 1) as cell_index
    from {{ ref('stg_importer_lots') }}
)

select week, importer, "row", cell_index, cell."column" as column_name, cell."text" as cell_text
from cells
