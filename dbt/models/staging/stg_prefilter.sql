-- The owner's keyword filters (the admin's Filters page; coffeedata.classify.KeywordFilter): one row per filter of this
-- build's snapshot (applies) or of the served build's (baseline), from a Parquet file coffeedata.decisions wrote
-- after checking the snapshot's digest (var prefilter_path). A filter that applies extends classify.excluded in
-- stg_products: a product it matches is never a coffee and gets no Claude verdict. The baseline rows let
-- eval_prefilter tell what this build's filters newly exclude. The build never reads the admin's database. No file:
-- no filters.
{%- set path = var('prefilter_path', '') %}
{%- if path %}
select phrase, field, applies, baseline, field || ':' || phrase as filter_key
from read_parquet('{{ path }}')
{%- else %}
select null::varchar as phrase, null::varchar as field, null::boolean as applies, null::boolean as baseline,
       null::varchar as filter_key
where false
{%- endif %}
