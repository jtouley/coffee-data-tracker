# Typed-text matching evaluation

This evaluates `shots.match_coffee`, which matches what a person types for a bag or shot to a catalog coffee and
returns its coffee_id, or nothing when the match isn't confident. It runs against a local, read-only copy of the
lake. It was made for #57, which widens `fuzzy_match_coffee`'s candidate pool, and follows the root `CLAUDE.md`.
The results are in `RESULTS.md`.

## Data

- Source: `marts/dim_coffee.parquet` from `hf://datasets/jtouley/coffee-data`, copied into a scratch folder.
  44,293 coffees from 1,848 roasters; sha256 `a87c87420a78b3c5a75b83d9f63424361ff8c9131a915a87f4253ad1c46b4292`.
  Nothing was written to the remote.
- Out of git: the catalog, the data dir's `split.json`, and the train and validation predictions.
- In git: `test_keys.txt` (the 1,000 test coffee_ids), the sealed test predictions and `SHA256SUMS`. The
  predictions are coffee_ids only. The scorer reads titles and roasters from the catalog.

## Split

- Rule: `n = int(sha256("coffeedata-match-eval-2026-10-30:" + roaster_id)[:8], 16) % 100`. `n < 60` is train,
  `n < 80` is validation, and the rest is test. Salt: `coffeedata-match-eval-2026-10-30`.
- It's grouped by roaster, so one roaster's coffees never sit on both sides. The one coffee with no title is left out.
- Result: train 26,319 coffees (1,085 roasters), validation 9,367 (368), test 8,606 (395).
- Each set's first 1,000 coffees by `sha256(salt + ":" + coffee_id)` are drawn. That's a uniform sample within
  the set, not a stratified one, so no stratum weights apply.
- The split was made before any prediction was read.

## Queries

Each drawn coffee gives two queries, both close to what people type:

- **title**: the coffee's title, as listed;
- **roaster+title**: `"<roaster_name> <title>"`.

## Reference (self-labelling)

The answer for a query is the coffee it was made from. Titles and roasters are compared after
normalization: casefold, then keep only the word characters, separated by single spaces.

- **right**: the predicted coffee has the same normalized roaster and title as the query's coffee. So a pick
  among identical lots (one roaster listing the same title twice) counts as right.
- **abstain**: no prediction (`None`). The person picks, or Claude does.
- **confidently wrong**: a prediction of any other coffee.
- **Ambiguous title-only queries.** When the query's normalized title is listed by more than one roaster, the
  text can't say which coffee is meant. Here abstaining counts as **right**, and any confident pick, even the
  query's own coffee, counts as **confidently wrong**: it's a guess. That follows `CLAUDE.md`: abstaining is
  better than guessing. A roaster+title query is never ambiguous in this sense.

The labels are mechanical, worked out from the catalog by the rules above, with no judgement involved. So
`CLAUDE.md` step 6's double labelling and settling doesn't apply. No person or model labelled anything.

Limits:

- These are exact catalog titles, so they're cleaner than real typing (misspellings, partial names, a roaster's
  nickname). The eval measures whether the matcher finds a coffee the text fully names, which is the failure #57
  fixes. It doesn't measure robustness to typos.
- The two queries from one coffee are correlated. The "all" row's interval treats them as independent, so read
  the per-form rows for the tighter claim.

## Protocol

1. **Train:** the only set read example by example (`score … train … --examples N`).
2. **Validation:** reported as aggregates. #57's code was already frozen, so there was nothing to choose between.
3. **Test (sealed):**
   - The code was frozen at `711134a`.
   - `pred_main_test.json` (main at `4e40ce7`) and `pred_pr57_test.json` were then written, and recorded in
     `SHA256SUMS`, in a commit of their own (`4d04daa`) before any test score was printed.
   - The test set was then scored once.
   - Check the files with `sha256sum -c SHA256SUMS`, run from this directory.
4. **Ship rule:** test right at least as high as main's, with no meaningful rise in confidently wrong.

## Commands

From this directory, with `PYTHONPATH` pointing at the `src` of the code being predicted (main's or the branch's):

```
python match_eval.py split   <lake-dir> <data-dir>
python match_eval.py predict <lake-dir> <data-dir> <set> <name>                         # train, validation
python match_eval.py predict <lake-dir> <data-dir> test <name> --out pred_<name>_test.json
python match_eval.py score   <lake-dir> <data-dir> <set> main pr57 [--examples N]       # --examples: train only
```

Test scoring, which prints only totals and Wilson 95% intervals:

```
python match_eval.py score <lake-dir> <data-dir> test main pr57
```

## Rule 9: running total of changed outputs

Products whose `match_coffee` output has changed since this field's last full evaluation: **0** (reset by #57's
full evaluation). Add each later change's count here.
