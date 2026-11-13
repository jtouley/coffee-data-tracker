-- The detector (design section 3): searches per week whose text looks like an email address, a phone number or a
-- card number. It returns only the week, the pattern's name and the count, never the text, and fails at any count
-- above 0; usage_build turns that into the triage alert "N searches in week W matched the <pattern> pattern" and
-- nothing more. Bots count too: the text is stored either way. store_failures stays off (dbt_project.yml), so no
-- failing row is ever written anywhere.
{{ config(severity='error', store_failures=false) }}
with searches as (
    select week, query from {{ ref('stg_usage_events') }} where query is not null
),

phone_or_card as (
    select week, query,
        -- a phone number written as one: one run of 7 to 15 digits, digits split by - or . into three groups, a
        -- leading +, a bracketed area code, or 3-3-4 digits split by spaces. Numbers that merely sit side by side
        -- ("gesha 2023 2024") don't count.
        regexp_matches(query, '(^|[^0-9])[0-9]{7,15}([^0-9]|$)')
        or regexp_matches(query, '(^|[^0-9])[0-9]{2,4}[.-][0-9]{3,4}[.-][0-9]{3,4}([^0-9]|$)')
        or regexp_matches(query, '\+[0-9][0-9 ().-]{5,}[0-9]')
        or regexp_matches(query, '\([0-9]{2,4}\) ?[0-9]{3}[ .-]?[0-9]{3,4}')
        or regexp_matches(query, '(^|[^0-9])[0-9]{3} [0-9]{3} [0-9]{4}([^0-9]|$)') as phone,
        -- a card number: 13 to 19 digits, with spaces or dashes between groups, that pass the Luhn check (which
        -- harvest years in a row almost never do)
        list_bool_or(list_transform(
            list_transform(regexp_extract_all(query, '[0-9][0-9 -]{11,}[0-9]'),
                           lambda m: regexp_replace(m, '[ -]', '', 'g')),
            lambda d: length(d) between 13 and 19
                and list_sum(list_transform(list_reverse(string_split(d, '')),
                    lambda x, i: if(i % 2 = 0,
                                    if(cast(x as integer) * 2 > 9, cast(x as integer) * 2 - 9, cast(x as integer) * 2),
                                    cast(x as integer)))) % 10 = 0
        )) as card
    from searches
),

matched as (
    select week, 'email' as pattern from searches where regexp_matches(query, '\S+@\S+\.\S+')
    union all
    select week, 'phone' from phone_or_card where phone
    union all
    select week, 'card' from phone_or_card where coalesce(card, false)
)

select week, pattern, count(*) as searches
from matched
group by week, pattern
