select roaster_id, run_date, count(*) from {{ ref('stg_runs') }} group by all having count(*) > 1
