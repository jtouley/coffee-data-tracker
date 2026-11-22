-- Claude's decision on each screen the rules left undecided (raw screen_verdicts; screen_verdict.py), for the model
-- and prompt version the build is configured with (vars screen_verdict_model, screen_verdict_definition): another
-- model's or an older prompt's answers are kept in the lake and left out here. A screen answered twice under the same
-- model and version keeps its first answer, the same one on every build.
select screen_id, roaster_id, evidence_hash, model, definition, label, reason, decided_at
from {{ lake('screen_verdicts') }}
where screen_id is not null
  and model = '{{ var("screen_verdict_model") }}'
  and definition = '{{ var("screen_verdict_definition") }}'
qualify row_number() over (partition by screen_id order by decided_at, label, reason) = 1
