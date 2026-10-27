-- Latest record per shot_id (corrections re-log the same id); retracted shots drop out. logged_at stays, so the
-- app can start from the fct_shot export and read only the shot files written after it. Shots from before
-- accounts existed have no user_id and are the owner's.
select * exclude (deleted) replace (coalesce(user_id, '{{ var("owner_user_id") }}') as user_id)
from {{ lake('shots') }}
where shot_id is not null
qualify row_number() over (partition by shot_id order by logged_at desc) = 1 and not deleted
