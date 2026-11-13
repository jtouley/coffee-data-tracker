-- The detector (design section 3): searches per week whose text looks like an email address, a phone number or a
-- card number. It returns only the week, the pattern's name and the count, never the text, and fails at any count
-- above 0; usage_build turns that into the triage alert "N searches in week W matched the <pattern> pattern" and
-- nothing more. Bots count too: the text is stored either way. store_failures stays off (dbt_project.yml), so no
-- failing row is ever written anywhere.
{{ config(severity='error', store_failures=false) }}
with searches as (
    select week, query from {{ ref('stg_usage_events') }} where query is not null
),

matched as (
    select week, 'email' as pattern from searches where regexp_matches(query, '\S+@\S+\.\S+')
    union all
    select week, 'phone' from searches
    where regexp_matches(regexp_replace(query, '[+().\s-]', '', 'g'), '[0-9]{7,}')
    union all
    select week, 'card' from searches
    where regexp_matches(regexp_replace(query, '[\s-]', '', 'g'), '(^|[^0-9])[0-9]{13,19}([^0-9]|$)')
)

select week, pattern, count(*) as searches
from matched
group by week, pattern
