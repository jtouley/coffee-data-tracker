-- Latest record per equipment_id. Retired equipment stays, so old shots still resolve their grinder.
select * exclude (logged_at) replace (coalesce(user_id, '{{ var("owner_user_id") }}') as user_id),
    logged_at as updated_at
from {{ lake('equipment') }}
where equipment_id is not null
qualify row_number() over (partition by equipment_id order by logged_at desc) = 1
