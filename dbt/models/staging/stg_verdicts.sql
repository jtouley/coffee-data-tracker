-- Claude's latest verdict per product version, for the model and definition the build was given (verdict.py).
-- Without a verdict model this is empty and the build uses the rules alone, which verdicts_cover_catalog fails
-- unless rules only was explicitly allowed (require_verdicts false). Two verdicts stored in the same second: the label
-- decides, so the pick never depends on the order rows are read.
select input_hash, label, model
from {{ lake('verdicts') }}
where model = '{{ var("verdict_model") }}' and definition = '{{ var("verdict_definition") }}'
qualify row_number() over (partition by input_hash order by decided_at desc, label) = 1
