-- Warn only (eval/dq/README.md, #4): the release-against-release reconciliation is measured on one recorded pair
-- of releases so far (956abd -> 5060ad); a rule warns until its evaluation shows it can block.
{{ config(severity='warn', store_failures=true) }}
-- Every row this build's public tables add, remove or change against the release customers get now must have a
-- reason the build can show (models/marts/release_changes.sql). One row per unexplained change.
select * from {{ ref('release_changes') }} where reason = 'unexplained'
