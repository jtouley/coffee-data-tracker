# PR-review model evaluation

This chooses the free OpenCode Zen model for the advisory PR review (`.github/workflows/pr-review.yml`). It
follows the root `CLAUDE.md`. The question is how often each model catches a bug in a real pull request, and how
often it blocks a pull request that's fine.

The reviewer is the same in CI and here:
- the prompt is `.opencode/agents/pr-reviewer.md`, the `/pr-review` command from jtouley/md_data_explorer adapted
  to take the diff in the message and to end with a JSON block;
- the diff preparation and the run are `tools/pr_review/pr_review.py`;
- OpenCode is `opencode-ai@1.19.5`, and the model gets no tools.

So the eval also compares prompts: put another review skill in the agent file and run the same sets.

## Data

- Source: every merged PR on the first-parent history of `main` in two repos, as of the commits in
  `manifest.json`:
  - jtouley/coffee-data-tracker at `28a1b05`;
  - jtouley/md_data_explorer at `b165635`.
- A PR is either a merge commit ("Merge pull request #N", diffed `base...head`) or a squash merge ("… (#N)",
  diffed against its parent).
- 41 of the 129 PRs are left out because no added line is non-test code to plant a bug in (docs, config, tests
  only). Their reasons are in `manifest.json`.
- Large diffs are kept. They're cut down exactly as CI cuts them (120,000 characters, 600 lines a file, no
  lockfiles or fixtures). Bugs are planted only in lines the reviewer sees.
- In git:
  - `manifest.json`: the PRs, their commits and split, and the planted bugs;
  - `test_items.sha256`: the sha256 of each test item's diff;
  - `SHA256SUMS`, over both.
- Out of git: the items, which `items` rebuilds from git history, and the train and validation predictions.

## Split

- Rule: `n = int(sha256("coffeedata-prreview-eval-2026-11-04:" + "<owner/repo>#<number>")[:8], 16) % 100`.
  `n < 60` is train, `n < 80` is validation, and the rest is test. Salt: `coffeedata-prreview-eval-2026-11-04`.
- It's grouped by PR, so a PR's clean diff and its planted-bug diffs are always on the same side.
- It was made with `draw`, before any model was run:

| set | PRs | items | with a planted bug |
|---|---|---|---|
| train | 50 | 147 | 97 |
| validation | 17 | 49 | 32 |
| test | 21 | 59 | 38 |

It isn't stratified, so no stratum weights apply.

## Items and the reference

Each PR gives up to three items:
- **clean**: the PR's diff as merged;
- **m1, m2**: the same diff with one planted bug each, a single-operator change to one added line of non-test
  code.

The planted bugs:
- The operators are mechanical:
  - flipped comparisons (`<`/`<=`, `==`/`!=`, `===`/`!==`);
  - `and`/`or`;
  - a dropped `not`;
  - `is None`/`is not None`;
  - `True`/`False`;
  - `min`/`max`;
  - `+ 1`;
  - in SQL, `left join` to `join`, `is null` flips, `count(distinct …)` and `asc`/`desc`.
- Strings, comments and docstrings are left alone.
- A line is chosen by a salted hash, and a PR's two planted bugs sit in different files or more than 20 lines
  apart.

The labels are mechanical: the planted bug is the answer. So `CLAUDE.md` step 6's double labelling doesn't apply,
and no person or model labelled anything.
- A review **catches** a planted bug when one of its findings is in the same file, within 3 lines of it, at any
  severity.
- **Background**: the same check run on the PR's clean review, at the planted bug's spot. It's the rate a model
  scores just by flagging that spot anyway. **Net catch** = catch − background.
- **Clean block rate**: NO MERGE on a clean PR. These PRs were merged after review. Some may still hold real bugs,
  so this is an upper bound on false blocks. It's the "confidently wrong" measure.
- **Answered**: the model answered with a valid JSON block. Free models rate-limit and come and go. Each item gets
  3 tries, and a model is stopped after 5 unanswered items in a row.

## Choosing a model (fixed before any run)

1. Score every free model on validation.
2. A model is eligible if it answers at least 90% of items.
3. Rank by net catch on answered items. Between models whose catch intervals overlap, prefer:
   - the lower clean block rate;
   - then the lower median seconds.
4. CI's `PR_REVIEW_MODELS` gets the top model, with the runner-up as fallback.

## Protocol

1. **Train:** the only set read example by example (`score … train … --examples N` shows missed bugs).
2. **Validation:** aggregates only. Choose with the rule above.
3. **Test (sealed):**
   - After the choice is frozen, run test for every model (`set: test`). The workflow uploads the predictions
     and doesn't score them.
   - Commit them as `pred_test/<model>.jsonl` and add them to `SHA256SUMS` in a commit of their own, then score
     once.
   - Check the files with `sha256sum -c SHA256SUMS`, run from this directory.
4. **Ship rule:** the chosen model's test net catch is at least the provisional default's (`opencode/big-pickle`),
   with no meaningful rise in clean block rate.

Nothing has been run yet. This sandbox's anonymous free tier was used up, so the first run is in Actions. There,
the job signs in with its GitHub OIDC token, through an OpenCode credential that trusts runs on `main`; the eval is
run by hand from `main`. Locally, set
`OPENCODE_API_KEY`.

## Commands

From the repo root, with clones that have full history:

```
python eval/pr_review/pr_review_eval.py items <data-dir> \
  --repo jtouley/coffee-data-tracker=. --repo jtouley/md_data_explorer=<path>   # checks test_items.sha256
python eval/pr_review/pr_review_eval.py predict <data-dir> validation opencode/big-pickle
python eval/pr_review/pr_review_eval.py score   <data-dir> validation <data-dir>/pred/validation/*.jsonl
python eval/pr_review/pr_review_eval.py score   <data-dir> train <pred.jsonl> --examples 10   # train only
```

Or in Actions: **pr-review-eval** → Run workflow, with a set and a model list. It runs one job per model, three at
a time, and puts the validation table in the run summary.

Test scoring, once, which prints only totals and Wilson 95% intervals:

```
python eval/pr_review/pr_review_eval.py score <data-dir> test eval/pr_review/pred_test/*.jsonl
```

## Limits

- **Small sets.** Validation has 32 planted bugs and test has 38. Intervals are about ±15 points, so only large
  differences between models will show. Items from one PR are correlated, and the intervals treat them as
  independent.
- **Planted bugs aren't real bugs.** They are local, one-token logic errors. That's the kind a diff reviewer
  should catch. They say nothing about design or architecture feedback, which this eval doesn't measure.
- **Diff only.** The reviewer sees the diff, not the repo. That's the same in CI, by design: no tools and no
  untrusted checkout.
- **Contamination.** Both repos are public. A model may have seen the clean PRs, but not the planted bugs.

## Rule 9: running total of changed outputs

This is a new field, so there's no earlier evaluation. The first full evaluation is the one above. Prompt or
model changes after it are measured by rerunning validation and a newly drawn test set.
