-- Latest record per shot_id (corrections re-log the same id); retracted shots drop out.
select * exclude (logged_at, deleted)
from {{ lake('shots') }}
where shot_id is not null
qualify row_number() over (partition by shot_id order by logged_at desc) = 1 and not deleted
