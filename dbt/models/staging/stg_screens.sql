-- One row per screen of a discovery candidate's site (raw screens; docs/design/roaster-screen.md, section 2.4), as
-- the job recorded it. Nothing is judged here: the tier is derived downstream. A row without a screen_id (the lake's
-- zero-row schema file reads as none) is left out.
select run_date, batch_id, screen_id, screened_at, roaster_id, state, status, refusal, http_status, final_host,
    platform, page_size, coalesce(products_read, 0) as products_read, coalesce(page_full, false) as page_full,
    coalesce(page_timeout, false) as page_timeout, edge, home_title, home_text, requests, bytes, crawl_delay_s,
    duration_s, code_version
from {{ lake('screens') }}
where screen_id is not null and roaster_id is not null
