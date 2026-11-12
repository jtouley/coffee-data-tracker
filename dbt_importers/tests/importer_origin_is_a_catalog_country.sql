-- Every importer's origin is one of the catalog's countries (importer_rules.catalog_countries(): origins.COUNTRIES as
-- canonical_country reads them), never a place as written ("Sumatra"), an island two countries share or an unknown
-- spelling: those are null, with Cafe Imports' column kept in origin_as_written. Returns a row, and fails, for each
-- other value.
select week, importer, "row", origin
from {{ ref('importer_offers') }}
where origin is not null
    and origin not in (select value from {{ ref('stg_importer_vocab') }} where kind = 'catalog_country')
