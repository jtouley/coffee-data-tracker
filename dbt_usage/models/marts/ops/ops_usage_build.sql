-- One row per pinned geo file this build read (config/geo.lock): its key, file, release date and sha256, which
-- usage_build checked before dbt ran. Same raw plus the same rows here gives the same marts.
{%- set lock = var('geo_lock') %}
{%- if lock %}
select * from (values
{%- for key in lock | sort %}
    ('{{ key }}', '{{ lock[key]['file'] }}', '{{ lock[key]['release'] }}', '{{ lock[key]['sha256'] }}'){{ ',' if not loop.last }}
{%- endfor %}
) as t(geo_key, file, release, sha256)
{%- else %}
select null::varchar as geo_key, null::varchar as file, null::varchar as release, null::varchar as sha256
where false
{%- endif %}
