# Working in this repo

## Heuristics and models are measured, not argued

Classifying products, extracting origins, matching bags to coffees, choosing a Claude model or prompt: any change
whose quality is "how often is it right" is judged on held-out data, like a model. Hand-written examples show
where a rule can break; they don't show how often it does.

1. **Look at the real data first.** Copy the tables you need from the lake to a local, read-only folder in your
   scratchpad. Inspect them before writing queries: columns and types (`describe`), row counts, the values a field
   actually takes. Don't assume the shape. Never write to the remote lake, and keep downloaded data out of git.
2. **Split before you look at results.** Split deterministically: a salted hash of the item key, grouped by roaster
   so one roaster's products never sit on both sides. Use train 60%, validation 20%, test 20%. Record the rule and
   the salt in the eval's README.
3. **Train:** the only set whose examples you read and tune against.
4. **Validation:** choose between candidates on aggregates only, never example by example.
5. **Test (sealed):** scored once, after the change is frozen. Store its keys with a `SHA256SUMS` and don't read its
   examples while developing. After the final score it can join the regression fixtures, and a new test set is
   drawn for the next change.
6. **The reference:** two independent hand labels with a third to settle disagreements (`eval/sealed/GUIDE.md`), or
   an existing stronger signal such as Claude's enrichment, saying which.
7. **Report:** the current code against the change on every set, with Wilson 95% intervals and, for a stratified
   sample, weights by stratum size. A change ships only if its test score is at least as good as `main`'s, with no
   meaningful rise in confidently wrong answers. Where rules can't tell, abstaining (leaving the value for Claude) is
   better than guessing.
8. **Keep the bar:** unit tests keep the reproduced cases, and accuracy floors (`tests/test_classifier_accuracy.py`)
   are only ever raised.

Precedent: `eval/sealed/` and `eval/validation/` for the coffee classifier.
