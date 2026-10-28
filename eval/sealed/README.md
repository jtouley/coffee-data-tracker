# Sealed test set for the coffee classifier

600 products drawn from the live lake on 2026-10-27 (300 the rules kept, 300 they dropped), none of them in the
train or validation sets in `tests/fixtures/classifier_labels.jsonl`. Each was labeled twice, independently, against
`GUIDE.md`, without seeing any prediction; a third labeler settled the 14 disagreements (`settled.json`).

Rules for using it:

- Don't read the products or labels to tune rules, prompts or model choice. Choose on the validation set.
- Score once, after the classifier is frozen: `python score_sealed.py final <predictions.json>`. It prints only
  totals and 95% intervals.
- `SHA256SUMS` was written as each file was made; `sha256sum -c SHA256SUMS` (from this directory, after adjusting
  the paths) shows nothing changed since.
- After the final score, the set joins the regression fixture and a new sealed set is drawn for the next change.

`pred_rules.json` holds the rules' predictions as of commit c021723, frozen before any score was taken.
