-- Latest record per shot_id (corrections re-log the same id); retracted shots drop out. Shots from before
-- accounts existed have no user_id and are the owner's.
select * exclude (logged_at, deleted) replace (coalesce(user_id, '{{ var("owner_user_id") }}') as user_id)
from {{ lake('shots') }}
where shot_id is not null
qualify row_number() over (partition by shot_id order by logged_at desc) = 1 and not deleted
