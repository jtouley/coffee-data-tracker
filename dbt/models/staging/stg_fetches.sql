-- status: ok, error, unavailable (a closed, deactivated or password-protected store), or crashed (not fetched:
-- quarantined for the week after it was in flight in two attempts of its crawl that died). partial: an ok fetch
-- that read only part of the catalog (null in files from before raw contract 1.2.0: complete). challenge: a failed
-- fetch an edge answered with its anti-bot challenge instead of the catalog (http.is_challenge), by edge: SiteGround
-- (a recorded block with its sg-captcha header, raw 1.4.0), Cloudflare (cf-mitigated: challenge), else `other` (a
-- bare 202, as a block or in the error, the only trace older files keep).
select run_date, batch_id, fetched_at, roaster_id, adapter, status, error, products, coffees,
    coalesce(partial, false) as partial,
    case when status = 'error' then case
        when list_bool_or(list_transform(blocks, lambda b: b.sg_captcha is not null)) then 'siteground'
        when list_bool_or(list_transform(blocks, lambda b: lower(b.cf_mitigated) = 'challenge')) then 'cloudflare'
        when list_bool_or(list_transform(blocks, lambda b: b.status = 202))
            or regexp_matches(coalesce(error, ''), '\(a challenge\)|HTTP 202\b|answered 202\b') then 'other'
    end end as challenge
from {{ lake('fetches') }}
where roaster_id is not null
