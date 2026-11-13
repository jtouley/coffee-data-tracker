-- The origin spelling seed agrees with ISO 3166-1 (the iso_3166_1 seed): a country row's alpha-2 code exists, and its
-- alpha-3 code and short name are that code's, so a typo in the mapping can't publish a name ISO doesn't have. Other
-- kinds carry no code, name or region. One row per disagreeing spelling.
select s.raw_value, s.kind, s.alpha2, s.alpha3, s.iso_short_name
from {{ ref('origin_country_iso') }} s
left join {{ ref('iso_3166_1') }} i on i.alpha2 = s.alpha2
where (s.kind = 'country' and (i.alpha2 is null or s.alpha3 is distinct from i.alpha3
                               or s.iso_short_name is distinct from i.iso_short_name))
   or (s.kind <> 'country' and coalesce(s.alpha2, s.alpha3, s.iso_short_name, s.region) is not null)
