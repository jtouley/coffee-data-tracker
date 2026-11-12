-- status: ok, error, unavailable (a closed, deactivated or password-protected store), or crashed (not fetched:
-- quarantined for the week after it was in flight in two attempts of its crawl that died). partial: an ok fetch
-- that read only part of the catalog (null in files from before raw contract 1.2.0: complete). challenge: a failed
-- fetch an edge answered with its anti-bot challenge instead of the catalog (http.is_challenge), named only when a
-- recorded block names the edge: SiteGround (its sg-captcha header, raw 1.4.0) or Cloudflare (cf-mitigated:
-- challenge). A bare 202, or "HTTP 202" in the error, names no edge (hosts answering it may share nothing): null, a
-- plain failure (CLAUDE.md: abstain).
select run_date, batch_id, fetched_at, roaster_id, adapter, status, error, products, coffees,
    coalesce(partial, false) as partial,
    case when status = 'error' then case
        when list_bool_or(list_transform(blocks, lambda b: b.sg_captcha is not null)) then 'siteground'
        when list_bool_or(list_transform(blocks, lambda b: lower(b.cf_mitigated) = 'challenge')) then 'cloudflare'
    end end as challenge
from {{ lake('fetches') }}
where roaster_id is not null
