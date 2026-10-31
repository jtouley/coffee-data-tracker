-- Carry-forward (dim_coffee carried_forward) lists a roaster's last good catalog through a failed fetch, but must
-- never put an observation in history: every price event is from a counted run of its roaster (stg_runs), so a
-- week the roaster wasn't fetched has no "unchanged", price or availability event. Lists any other event.
select e.coffee_id, e.variant_id, e.run_date, e.event
from {{ ref('fct_price_event') }} e
left join {{ ref('stg_runs') }} r using (roaster_id, run_date)
where r.roaster_id is null
