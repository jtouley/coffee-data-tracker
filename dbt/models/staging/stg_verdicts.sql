-- Claude's latest verdict per product version, for the model and definition the build was given (verdict.py).
-- Without a verdict model the build uses the rules alone.
select input_hash, label
from {{ lake('verdicts') }}
where model = '{{ var("verdict_model") }}' and definition = '{{ var("verdict_definition") }}'
qualify row_number() over (partition by input_hash order by decided_at desc) = 1
