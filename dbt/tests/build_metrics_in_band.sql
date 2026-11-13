-- The macro's refs sit inside its var check (as_of_run), so dbt would see them only when the build's vars are
-- set; this line declares them whatever the vars (docs/dbt/CONVENTION.md, section 5).
-- depends_on: {{ ref('stg_builds') }}
-- depends_on: {{ ref('mart_build_profile') }}
-- Warn-only for now (the owner's call): a band out of range is reported, not blocking, until band_k is
-- recalibrated on 8-12 weeks of real history. The fixed floors (run_ok_rate, adapter_ok_rate,
-- catalog_volume_vs_last_build) block.
{{ config(severity='warn') }}
-- A build unlike the recent ones is flagged even when it clears the fixed floors: each banded metric
-- must lie within median +/- band_k x 1.4826 x MAD of its values in the recent passing builds (the history), or
-- band_min_*_halfwidth when that is wider (a flat history has MAD 0). Volume metrics fail either way; ok shares
-- only below (a platform getting better is not a problem). Lists each metric out of band.
--
-- The history is the latest passing build of each earlier run, at most band_window runs, from the last reset on.
-- A reset is a passing build let through with --accept-volume-change (metric accepted_volume_change) or the first
-- passing build that saw a new accept override (accepted_overrides went up): the history starts again at it, so a
-- real level change is accepted once, not every week after. A build that is itself such a reset is not banded.
-- Until a metric has band_min_builds builds of history, it is not banded: only the fixed thresholds apply
-- (run_ok_rate, adapter_ok_rate, catalog_volume_vs_last_build), and they apply as hard floors either way.
{{ metrics_out_of_band("r.metric in ('listed_coffees', 'in_stock_coffees', 'roasters_with_coffee', "
                         ~ "'run_catalog_ok_share') or r.metric like 'adapter_ok_share:%'") }}
