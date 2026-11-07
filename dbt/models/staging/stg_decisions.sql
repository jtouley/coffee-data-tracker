-- The owner's include and exclude decisions this build applies (docs/design/admin-registry.md §2.4), one row per
-- subject: a coffee (coffee_id) or a product version (input_hash, which every product sharing that version has).
-- They come from the snapshot the build was given (`build --snapshot`, a draft) or, for any other build, the served
-- build's (its builds row kind snapshot), as a Parquet file coffeedata.decisions wrote after checking the snapshot's
-- digest (var decisions_path). The build never reads the admin's database. No file: no decisions.
{%- set path = var('decisions_path', '') %}
{%- if path %}
select decision_id, subject_kind, subject, action, subject_kind || ':' || subject as subject_key
from read_parquet('{{ path }}')
{%- else %}
select null::varchar as decision_id, null::varchar as subject_kind, null::varchar as subject,
       null::varchar as action, null::varchar as subject_key
where false
{%- endif %}
