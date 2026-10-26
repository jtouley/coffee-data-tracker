select
    slug as roaster_slug, name as roaster_name, domain, nullif(city, '') as city, nullif(state, '') as state,
    sources, coalesce(try_cast(locations as integer), 1) as locations, platform, status,
    coalesce(is_chain, 'false') = 'true' as is_chain,
    coalesce(tagged_roastery, '') = 'true' as tagged_roastery,
    try_cast(nullif(last_probed, '') as date) as last_probed
from read_csv('{{ var("config_dir") }}/roasters.csv', header = true, all_varchar = true)
