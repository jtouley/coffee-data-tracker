-- Listed coffees whose title is still HTML-escaped or reads as a spare part, cup, cleaner or test listing.
-- stg_products runs the classifier over every snapshot, so none should get through.
select coffee_id, title from {{ ref('dim_coffee') }}
where is_listed and (
    title like '%&amp;%'
    or regexp_matches(title, '\b(burrs|gaskets?|portafilters?|saucers?|descal\w*|cleaning|(espresso|lungo|dosing) cups?)\b', 'i')
    or regexp_matches(title, '\[test\]|\be2e test\b', 'i')
)
