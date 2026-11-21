-- A screen sends at most 6 requests, robots.txt and redirects included (screen.MAX_REQUESTS;
-- docs/design/roaster-screen.md, section 3.1). A row above the cap means the job's cap didn't hold.
select screen_id, roaster_id, requests
from {{ ref('stg_screens') }}
where requests > 6
