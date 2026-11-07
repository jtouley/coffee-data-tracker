-- Warn only, and report only (eval/dq/README.md, #4): `coffeedata build` runs it after the gate (tag report_only),
-- so even an error here can't hold back a release. Measured on one recorded pair of releases (956abd -> 5060ad).
{{ config(severity='warn', store_failures=true, tags=['report_only']) }}
-- Every row this build's public tables add, remove or change against the release customers get now must have a
-- reason the build can show (models/marts/release_changes.sql), and the comparison must have been made in full:
-- one row per unexplained change, per compared column or table the served release lacks, and one when the served
-- release couldn't be read.
select * from {{ ref('release_changes') }}
where reason = 'unexplained' or change in ('schema', 'not_reconciled')
