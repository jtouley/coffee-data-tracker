# Working in this repo

## Heuristics and models are measured, not argued

Classifying products, extracting origins, matching bags to coffees, choosing a Claude model or prompt: any change
whose quality is "how often is it right" is judged on held-out data, like a model. Hand-written examples show
where a rule can break; they don't show how often it does.

1. **Look at the real data first.** Copy the tables you need from the lake (`$COFFEEDATA_LAKE`) into a local,
   read-only folder in your scratchpad. Inspect them before writing queries: columns and types (`describe`), row
   counts, the values a field actually takes. Don't assume the shape. Never write to the remote lake, and keep
   downloaded data out of git.
2. **Split before you look at results.** Split deterministically: a salted hash of the item key, grouped by roaster
   so one roaster's products never sit on both sides. Use train 60%, validation 20%, test 20%. Record the rule and
   the salt in the eval's README.
3. **Train:** the only set whose examples you read and tune against.
4. **Validation:** choose between candidates on aggregates only, never example by example.
5. **Test (sealed):** scored once, after the change is frozen. Score `main` and the change on the same frozen set.
   Write a `SHA256SUMS` over its files when you make them. Don't read its examples while developing. Keep its
   scoring command in the eval's README; it prints only totals and intervals. After the final score the set can
   join the regression fixtures, and a new test set is drawn for the next change.
6. **The reference labels:** each item is labeled twice, independently, against a written definition, and a third
   labeler settles disagreements. The first two labelers are different models (the same model gives the same
   answers), the third is a model different from both, and no labeler wrote or tuned the change being measured.
   Labelers see only the items and the definition, each in its own folder. Say who labeled (people or Claude, and
   which model). An existing signal such as Claude's enrichment can serve as the reference only when the change being measured isn't that same model or
   prompt.
7. **Report:** the current code against the change on every set, with Wilson 95% intervals and, for a stratified
   sample, weights by stratum size. A change ships only if its test score is at least as good as `main`'s, with no
   meaningful rise in confidently wrong answers, and its estimated accuracy over the full population of current
   items is at least 95%: measured on a sealed set stratified and weighted over the whole population, not only the
   changed slice, and held by a test. Floors on slice sets stay at their measured values; only the full-population
   estimate has the 95% bar. Where rules can't tell, abstaining (leaving the value for Claude) is better than
   guessing.
8. **Keep the bar:** unit tests keep the reproduced cases, and accuracy floors are only ever raised.
9. **Scale the effort to the change.** Keep a running total of products whose output has changed for each field
   since its last full evaluation, in that field's eval README. If, compared with `main`, the total including this
   change stays at 50 or fewer, steps 2–7 can be skipped. Instead:
   - add unit tests for the specific cases;
   - list the changed products and check each by hand (for sealed-test products, show only a count);
   - keep the existing floors and regression fixtures passing.

   A total over 50, or a change to how a rule decides rather than one case it gets wrong, needs the full process.

### The coffee classifier's evaluation

It's the working example of sealed scoring, labeling and floors:
- `eval/sealed/README.md` covers the procedure;
- `eval/sealed/score_sealed.py` covers agreement and final scoring;
- `eval/sealed/GUIDE.md` is the labeling definition;
- `tests/test_classifier_accuracy.py` holds the floors.

It predates this file, so its sets were drawn one after another as fixed-size samples (about 600 each), not by a
salted hash, and they aren't grouped by roaster. Its labels were made by Claude subagents; a spot-check by a person
is still to come. New evaluations follow the steps above.

## Changes are run, not eyeballed

Before review and before pushing, a dbt change runs a full build (`coffeedata build`, every gate test), and a
change to the site, the app, their templates, CSS or JS is driven end to end with Playwright:
- every state the change touches, at phone (390 px) and desktop widths, in light and dark;
- a slow server and the error cases;
- an axe check.

The design-skills review reads those screenshots.

## Reviews: one, fixes, one

1. **One specialist review**, with the skills the change needs (dbt and data quality, DevOps, design, security),
   alongside `/code-review` on the full PR diff at:
   - **low:** docs, specs, notes, copy;
   - **medium** (the default): ordinary features and fixes;
   - **high:** auth and security, money, anything that publishes data (dbt gates, public tables, contracts),
     terraform.

   A PR that mixes them takes the highest level that applies. It posts one full review as a PR comment, each
   finding tagged with its severity and source.
2. **One round of fixes.**
3. **One final review** (`/code-review` at low or medium): the fixes landed and nothing critical remains. If so,
   wait for CI and merge. The smoke test (`make smoke`, CI's `smoke` check) passes on every PR.

### Verify the premise first

Before reviewing or acting, verify the premise: don't act until each step that applies is done (not every review
has a failure to reproduce).
1. **Check for existing work:** open PRs, issues and discussions that already cover it. Point to one instead of
   duplicating it; this never means skipping the review.
2. **Find the real failure:** read the actual error, labels or logs. Don't trust a summary of why, an AI one included.
3. **Reproduce it yourself:** run the failing command or check on the current version; report the exact output and
   exit code.
4. **Check the source of truth:** the code, official docs, downloads, hashes, the maintainers' repo; not blog posts
   or AI overviews.
5. **Say who can actually fix it:** if the fix needs someone else's access or code, say so and where to report it.
6. **Only then propose the action,** with the evidence for each claim. Show the owner any public post outside this
   repo before it goes out.

Each review's PR comment ends with **Premise checks**: what was checked, what was found, and what wasn't verified.

## Owner-controlled state changes only through the admin API

Crawl on/off, chain and not-a-roaster flags, decisions, rule settings, approvals, publishes and rollbacks are the
owner's switches. They live in the admin's control plane (`admin/controlplane.py`; `docs/admin/DESIGN.md`), where
every change is an audited, reversible row.
- Change them only through the admin's API routes (for example `POST /api/roasters/{roaster_id}/crawl`), whether by
  hand or by script. A script calls the same authenticated route a person would.
- Never change them by editing a CSV, a dbt seed, the control-plane database or the lake, and never by a code list.
  A PR that removes, hides or reclassifies a roaster or coffee that way is wrong, whatever its evidence.
- If no route does what's needed, build one that follows the control plane and services model: a service function
  with its guards, one audit row per change, a way to reverse it, and the API route on top. It's reviewed at high.
- Until the admin is live, record what should change (ids, evidence, the reason to send) for the owner to apply;
  don't work around the missing API.

## Keep the repo clean

1. **No notes or scratch on any branch.** Working notes, plans, decision logs and agent state stay out of git (a
   scratchpad or somewhere private). Decisions go on the issue or PR they decide. No `notes/`, `tmp/`, `.context/`
   or diagnostic workflows, session branches included.
2. **One PR per independent change, off `main`.** Bundle tiny related changes into one PR. Stack only when a
   change truly depends on another, and at most 2 deep. Keep at most about 10 PRs open at once.
3. **Every PR is squash-merged and its branch deleted at merge,** the same hour it's approved with CI green. Delete
   a closed PR's branch when you close it. Name branches `<type>/<topic>` (`feat`, `fix`, `docs`, `eval`, `infra`,
   `deps`), and don't push a branch without opening its PR. The PR title becomes the commit: `Area: what changed`,
   under 72 characters, with no instructions in it ("do not merge ..."); those go in the body and a label.
4. **PR bodies under 40 lines, details in `<details>`.** The visible part says what and why, the effect on users or
   data, how it was tested, and the owner's steps. Evidence tables, premise checks and review history go in
   `<details>`. No local paths (`/home/...`, scratchpad, worktrees), internal tool headers or model names.
5. **Reviews are one comment each, readable on a phone:** a summary of at most 20 lines, findings in `<details>`,
   and no tooling boilerplate (`RUN_BY_COMMAND`, skipped-gate blocks).
6. **One issue per problem.** Review follow-ups are separate issues, not a list in one; search before opening one.
   A PR that fixes an issue says `Closes #n`.
7. **Triage issues belong to the workflows** that open and close them: comment, don't close them by hand.
8. **Drafts expire after 14 days.** A draft untouched for 14 days is closed, with an issue holding what's left.
9. **Nothing generated or one-off in the tree.** Review screenshots go in PR comments, one-off shell runs in the PR
   body, and scripts in `scripts/`, not `docs/`. Lake data stays out (the fixtures and eval sets CONTRIBUTING allows
   are the exception).
10. **One home per topic.** Architecture goes in `docs/ARCHITECTURE.md`. A change that makes a doc wrong fixes it in
    the same PR.
