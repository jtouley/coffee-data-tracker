# Sealed test set for the coffee classifier

600 products drawn from the live lake on 2026-10-27 (300 the rules kept, 300 they dropped), none of them in the
train or validation sets in `tests/fixtures/classifier_labels.jsonl`. Each was labeled twice, independently, by Claude subagents
against `GUIDE.md`, without seeing any prediction; a third labeler settled the 14 disagreements (`settled.json`).

Rules for using it:

- Don't read the products or labels to tune rules, prompts or model choice. Choose on the validation set.
- Score once, after the classifier is frozen: `python score_sealed.py final <predictions.json>`. It prints only
  totals and 95% intervals.
- `SHA256SUMS` was written as each file was made; `sha256sum -c SHA256SUMS` (from this directory, after adjusting
  the paths) shows nothing changed since.
- After the final score, the set joins the regression fixture and a new sealed set is drawn for the next change.

`pred_rules.json` holds the rules' predictions as of commit c021723, frozen before any score was taken.

**Running total (CLAUDE.md step 9):** 0 products whose `is_coffee` changed, counted from the change that started
this total; changes before it weren't counted. Add each later change's count here.
- decoding HTML entities in tags in dbt (`dbt-decode-tag-entities`) changed the tags of 1,087 products (1,090
  versions, 51 roasters) and the output of `is_coffee` of none: built from main and from the change on a local copy of
  the lake (runs 2026-10-26 to 11-02, rules only), `stg_products` and every mart and public table were identical. 0 of
  them in this sealed set.
