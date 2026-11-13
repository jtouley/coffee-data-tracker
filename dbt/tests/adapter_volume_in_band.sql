-- The macro's refs sit inside its var check (as_of_run), so dbt would see them only when the build's vars are
-- set; this line declares them whatever the vars (docs/dbt/CONVENTION.md, section 5).
-- depends_on: {{ ref('stg_builds') }}
-- depends_on: {{ ref('mart_build_profile') }}
-- Warn only (the owner's rule: a new rule warns until its evaluation shows it can block; eval/dq/README.md, #11).
{{ config(severity='warn') }}
-- One platform's collapse, against that platform's own history: the bands of build_metrics_in_band
-- (macros/metrics_out_of_band.sql: median - band_k x 1.4826 x MAD of the recent passing builds, history restarted
-- on an accepted change, judged only from band_min_builds builds and only for adapters with min_adapter_roasters
-- roasters with a catalog), low side only, with the minimum half-width band_min_adapter_halfwidth, on:
--   adapter_attempted_ok_share:<adapter>: the platform's fetches failing (blocks, challenges, rate limits), without
--     the false alarm adapter_ok_share gives on every platform when a run just tries fewer roasters;
--   adapter_same_store_products:<adapter>: fetches that "succeed" with fewer products than the same stores had;
--   adapter_same_store_priced:<adapter>: the same stores' priced products: a price wipe on one platform.
-- Lists each metric out of band.
{{ metrics_out_of_band("r.metric like 'adapter_attempted_ok_share:%' or r.metric like 'adapter_same_store_%'",
                       min_share_halfwidth=var('band_min_adapter_halfwidth')) }}
