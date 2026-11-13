-- Warn only, always (the owner's call): unlike build_metrics_in_band, this band never blocks a release, even once
-- band_k is recalibrated. A fixed baseline that blocked was tried and rejected for failing real weekly changes; the
-- enrichment backlog (capped per week, collected next run) and partial fetches move these shares legitimately.
{{ config(severity='warn') }}
-- The same bands as build_metrics_in_band (macros/metrics_out_of_band.sql: median +/- band_k x 1.4826 x MAD of
-- the recent passing builds, band_min_share_halfwidth at least, history restarted on an accepted change, banded
-- only from band_min_builds builds), low side only, on these mart_build_profile shares of the listed coffees:
--   filled_share:<column> (origin_country, process, roast_level, price_per_100g): a drop is an extraction or
--     enrichment break, which accepted_values lets through as nulls;
--   listed_seen_this_run_share: a drop means fewer of the listed coffees came from this run (more partial or
--     failed fetches keeping last week's listing).
{{ metrics_out_of_band("r.metric like 'filled_share:%' or r.metric = 'listed_seen_this_run_share'") }}
