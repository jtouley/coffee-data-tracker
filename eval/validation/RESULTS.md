# Choosing the verdict model (validation set), 2026-10-28

The 597 validation products in `tests/fixtures/classifier_labels.jsonl`. Products the rules exclude outright
(`classify.excluded`, 243) are never sent; the other 354 were judged by each model against
`src/coffeedata/coffee_definition.md`, blind to labels, through Claude Code subagents on the owner's subscription
(the same models and definition as `verdict.py`, but not the exact API request). "unsure" falls back to the rules,
as in the build. Weighted = each stratum (kept / dropped by the rules) weighted by its size in the catalog.

| Classifier | Wrong of 597 | Weighted accuracy | 95% CI |
|---|---|---|---|
| Rules only | 38 | 94.0% | 90.4–96.2% |
| claude-haiku-5-5 | 6 | 99.0% | 96.8–99.7% |
| claude-sonnet-5-5 | 4 | 99.1% | 97.0–99.7% |
| claude-opus-5-5 | 3 | 99.3% | 97.3–99.8% |

Rule agreed beforehand: the cheapest model at or above 98% on validation. **Frozen: claude-haiku-5-5**, before the
sealed test set was scored. The labels were also made by Claude, so shared blind spots may inflate all three; an
owner spot-check of the labels is the guard.

`pred_<model>_<k>.json` are the raw labels by listing id.
