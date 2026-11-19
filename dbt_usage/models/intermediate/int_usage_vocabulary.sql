-- One row per word of the served release's vocabulary: every word of a listed roaster's name, city and state, and
-- of a coffee's title, origin and process, lowercased without accents. A query with a word outside it needs more
-- days to be shown (design section 3).
with words as (
    select unnest(regexp_split_to_array(lower(strip_accents(words_text)), '[^\p{L}\p{N}]+')) as word
    from {{ ref('stg_usage_catalog') }}
)

select distinct word from words where word <> ''
