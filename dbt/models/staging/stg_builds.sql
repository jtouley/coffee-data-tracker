-- One row per recorded build, and whether it passed: its 'pass' verdict and no failed or errored node. This is the
-- one definition of a passing build (quality.builds() applies the same rule; tests/test_quality.py checks they
-- agree). Builds are ordered by built_at, then build_id.
select
    build_id, any_value(run_date) as run_date, max(built_at) as built_at,
    bool_or(kind = 'build' and status = 'pass') and not bool_or(coalesce(status, '') in ('error', 'fail')) as passed
from {{ lake('builds') }}
where build_id is not null
group by build_id
