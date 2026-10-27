-- Latest record per bag_id (finishing or correcting a bag is a new record); deleted bags drop out.
select * exclude (logged_at, deleted) replace (coalesce(user_id, '{{ var("owner_user_id") }}') as user_id),
    logged_at as updated_at
from {{ lake('bags') }}
where bag_id is not null
qualify row_number() over (partition by bag_id order by logged_at desc) = 1 and not coalesce(deleted, false)
