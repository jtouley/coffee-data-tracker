-- Every stored lot is an offer, once: importer_offers has as many rows as stg_importer_lots, so no lot drops out of
-- an inner join (a lot stored with no cells stays, its values null). Returns a row, and fails, when the counts differ.
with counts as (
    select
        (select count(*) from {{ ref('stg_importer_lots') }}) as lots,
        (select count(*) from {{ ref('importer_offers') }}) as offers
)

select lots, offers from counts where lots != offers
