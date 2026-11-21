-- A suggested place never carries a reseller doubt: another roaster's name on its coffees, three or more vendors, a
-- "proudly serving" phrase, or a wholesale-only catalog (screen_rules.RESELLER_CODES; docs/design/roaster-screen.md,
-- section 4.2, rule 3). A doubt always goes to review.
select roaster_id, screen_id, reason
from {{ ref('screen_scores') }}, unnest(reasons) as t(reason)
where tier = 'suggested'
  and regexp_matches(reason, '^(other_roaster|vendors|serves|wholesale)')
