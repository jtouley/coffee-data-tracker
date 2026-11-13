# "Your Next Bag": pre-registration of the five-rating recommendation experiment

Written on 2026-11-13, before any rating, slate or response exists. It fixes the question, the arms, how
coffees are assigned and shown, who takes part, what is measured, what result counts as a pass and when the
experiment stops. Nothing here changes once it is sealed (see **The seal** below). The design it comes from was
approved by the owner on 2026-11-13. The owner's defaults are built in: arms are interleaved within each user,
"popular" is site click-outs (with a named fallback), and nobody has to buy a bag.

**Running total (CLAUDE.md step 9):** not applicable. This experiment doesn't change any product's output. It
measures whether a new feature is worth building.

## The question and the hypothesis

**Question.** Can five coffee ratings produce suggestions that a person prefers to a popular-coffee baseline?

**Hypothesis.** If a user's five ratings drive a content-based slate (C), then the user will rate its coffees "Want
to try" more often than the most popular coffees (P). The reason: origin, process, roast, variety and tasting notes
carry signal about taste.

**The null.** For each user, C and P are equally likely to win.

## How this maps onto CLAUDE.md's measurement rules

| CLAUDE.md | Here |
|---|---|
| Train | The pilot: the owner and 2 pilot users. Their slates may be read freely, and weights may be tuned on them. They are never scored. |
| Validation | None. There is only one candidate. If a weight changes after the pilot, it changes on pilot aggregates only, and the spec is sealed again before the first cohort invite. |
| Test (sealed) | The cohort. `score_recs.py` runs once, after the last window closes, and prints only totals, intervals and the test result. |
| Reference labels | Each user's own answers. No labelers are needed. |
| Split by salted hash | The **assignment** uses a salted hash per user (below). The user is the unit; there is no item split. |

## Arms

Every user sees **six coffees: two from each arm**, mixed together. No card carries an arm label, a score or a
"because you liked…" line.

- **C, content.** Nearest neighbours on catalog attributes (`config/recs.yml` → `content`).
  - Each coffee is a sparse vector of (feature, weight) pairs in five blocks: origin country (with region at half
    weight), process, roast level, varietals and flavor categories.
  - Multi-valued blocks are IDF-weighted. Every block is L2-normalised to the same weight, so a coffee without notes
    isn't penalised.
  - Profile = Σ (rating − the user's mean of their five ratings) × the coffee's vector. If all five ratings are equal,
    the profile is the plain sum.
  - Score = cosine(profile, coffee). Ties go to the lower `coffee_id`.
- **P, popular.** The highest-ranked eligible coffees in the frozen popularity snapshot.
  - **Source (owner's default):** site click-outs over the trailing 8 weeks, frozen at the seal. This applies once
    the analytics design is live and the window holds at least 300 click-outs. Coffees are ranked by click-outs,
    descending.
  - **Fallback, used if click-outs aren't available at the seal:** the **longest-listed in-stock coffees** in the
    pinned release, ranked by `dim_coffee.weeks_listed` descending, then `first_seen` ascending, then `coffee_id`
    ascending. These are the roasters' staple coffees.
  - The seal records which source was used. Either way, the ranking is one fixed file, and its sha256 is pinned.
- **R, random.** A uniform draw from the eligible coffees, seeded per user: coffees are ordered by
  `h(salt|user_id|R|coffee_id)` ascending.

**Eligible coffees** (per user, all from the pinned catalog release, `config/recs.yml` → `eligibility`):
- `is_coffee`, retail (not `is_wholesale`), and `in_stock` in the pinned release (never live stock);
- at least two of the five content blocks present;
- not one of the user's five rated coffees;
- not decaf, unless the user rated a decaf;
- at most one coffee per roaster in a slate.

## Assignment: team-draft interleaving by salted hash

`h(x)` is the lowercase hex SHA-256 of the UTF-8 string `x`. Hashes are compared as strings, so ascending string
order is the same as ascending numeric order. `|` is a literal separator.

1. Each arm ranks the user's eligible coffees, as described above.
2. **Draft:** there are two rounds, `r = 1, 2`. In round `r`, the three arms take turns in ascending order of
   `h(salt|user_id|draft|r|arm)`. On its turn, an arm takes the first coffee on its list that isn't already in the
   slate and whose roaster isn't already in the slate. That coffee belongs to that arm, so a coffee wanted by two
   arms goes to whichever drafts first, and the other arm takes its next choice.
3. **Positions:** the six drafted coffees are shown in ascending order of `h(salt|user_id|slot|coffee_id)`. The
   draft order says nothing about where a card appears.

The slate is a pure function of the raw seed ratings, the pinned release, the pinned popularity file and the salt.
There is no wall-clock time and no live stock, so a rebuild gives the same slate (the owner's idempotency rule).

### The salt: sealed by hash, revealed at the end

The repo is public. With a committed salt and the public code, anyone who knew a participant's `user_id` could work
out which card came from which arm. The consent line promises participants that they aren't told this until the
test ends. So:

- **Cohort salt.** At the seal, the owner generates 32 random bytes, written as 64 hex characters, for example with
  Python's `secrets.token_hex(32)`. The salt is stored only as a private secret for the nightly rec build, never in
  git, logs or any public table. **Only `sha256(salt)` is committed**, in the seal table below. At 256 bits, the
  salt can't be recovered from its hash.
- **Revealed at the end.** After the scoring run, the salt is written into `RESULTS.md`. Anyone can check that its hash
  matches the sealed one and rebuild `mart_rec_arm` from it.
- **Pilot salt.** This salt is committed, because pilot users are unblinded and never scored:
  `coffeedata-recs-pilot-2026-11`.
- **A fresh cohort** (see **Outcomes**) gets a new salt, sealed the same way before its first invite.

## Participants

**Eligible users:**
- joined through an invite made for this cohort;
- recorded the `experiment` consent at this spec's `experiment_version`;
- self-report, as the invite states, that they buy specialty beans from US roasters at least monthly and can name
  five coffees they drank in the past year (stated in the invite, not verified);
- saved five seed ratings of registry coffees.

The **training data** are the user's first five seed ratings as they stood when the fifth was saved. Later edits or
ratings don't change the slate.

**Never eligible:** the owner, the pilot users, and anyone who has worked on the recommendation code or this spec.

**Target:** 30 invites, for about 20 users who send their slate. Participants **don't have to buy a bag** (owner's
default). The primary metric is the choice, and the later rating is optional.

## Exclusions (decided before unblinding)

Each exclusion is decided from fields that carry no arm and no response: invite, consent, account and slate-build
facts. Each excluded user is listed with a reason in the exclusions file, and the scorer prints only the count for
each reason. A user is always dropped whole, never item by item.

1. The owner, the pilot users and contributors (see above).
2. A second account belonging to the same person. The account whose consent came first is kept.
3. Withdrawn consent or erased data before the scoring run. Their rows are gone, and they're counted in the funnel.
4. **Slate not sent** before the cohort closes. The scorer applies this rule itself.
5. **Malformed slate:** not six coffees, not two per arm, a repeated coffee, or not exactly one "buy next" pick.
   This is a build fault. The scorer applies this rule itself and reports the count.

**Nothing else is excluded.** That means no outlier removal, and no dropping of users whose answers look careless
(all "Maybe", say). A user with equal C and P interest is a **tie**: they stay in the counts and drop out of the
sign test, as the test defines.

## Metrics

**Response coding:** "Want to try" = 2, "Maybe" = 1, "Not for me" = 0. Each user also makes one "I'd buy this
next" pick. **T** is the date of the nightly build that first contains the user's slate.

| Metric | Definition | Role |
|---|---|---|
| **Paired preference, C vs P** | For each user, the mean interest of their 2 C items minus that of their 2 P items. `k` = users with C > P, `n` = users with C ≠ P. Exact two-sided binomial sign test against ½, α = 0.05. **Met when p < 0.05 and k > n − k.** | **primary** |
| C vs R | The same comparison, C against R. Only the direction counts (k > n − k). No significance is required. | sanity: C must beat random |
| "Not for me" rate | For each arm, "Not for me" items ÷ that arm's items, with Wilson 95% intervals. **Guardrail:** C's rate minus P's rate is at most **10 percentage points** (point estimates). | guardrail |
| Pick share | For each arm, the share of "buy next" picks, with Wilson 95% intervals. Chance is 1/3. | secondary, descriptive |
| Later rating | 1–5 ratings of slate coffees the user drank, logged after T and by T + 35 days (the latest per user and coffee). For each arm: n, mean, and the share ≥ 4 with a Wilson 95% interval. | secondary, descriptive |
| Ranking of later coffees | Covers coffees the user rates in the same window that weren't in the slate or the seed. For each user with at least two differently rated coffees: the share of differently rated pairs that C's score orders correctly, against the share that P's popularity orders correctly (ties count as half; a coffee absent from the snapshot ranks last). Reports the users for whom C does better, with a Wilson 95% interval over non-tied users. | secondary, descriptive |
| Completion | invited → five ratings → slate sent → a later rating | funnel, for planning the next cohort |

The "Not for me" intervals treat items as independent, but each user contributes two items per arm. So the
intervals are a little too narrow. The guardrail therefore uses point estimates only.

## What 20 users can and can't detect

Threshold for the primary result: the fewest of `n` non-tied users who must favour C. All values are exact binomial
calculations, and `score_recs.threshold` reproduces them.

| non-tied users n | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | **20** | 21 | 22 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| must favour C | 10 | 11 | 12 | 12 | 13 | 13 | 14 | 15 | **15** | 16 | 17 |

Power at n = 20 (threshold 15), by the true share of non-tied users who prefer C (`score_recs.power`):

| True share preferring C | 60% | 70% | 75% | 80% | 85% |
|---|---|---|---|---|---|
| Power at n = 20 | 13% | 42% | 62% | 80% | 93% |

Plainly: **20 users can confirm that about four in five people prefer C. They can't detect a 60/40 or even a 70/30
preference,** so a miss is weak evidence against C. 15 of 20 has a Wilson 95% interval of 53–89%. The later ratings
(about 10–30 bags) are descriptive only: 7 of 20 rated ≥ 4 has an interval of 18–57%.

## Outcomes (decided before any data)

The outcomes are checked in this order. `score_recs.outcome` implements this table, and its tests cover each
boundary.

| Outcome | When | What happens |
|---|---|---|
| **INCONCLUSIVE, too few** | Fewer than **12** non-tied C-vs-P users | No verdict. The owner may run the one fresh cohort described below. |
| **FAIL** | C ≤ P in direction (k ≤ n − k), **or** C ≤ R in direction | Stop recommendation work and return to insights (the product order in NEXT-STEPS). |
| **INCONCLUSIVE, guardrail** | The primary is met and C is ahead of R, but C's "Not for me" rate is more than 10 points above P's | Not built. The owner reviews the aggregates. Any re-run needs a re-sealed spec and a fresh cohort. |
| **SUCCESS** | The primary is met (≥ 15 of 20, or the threshold above for the n left after ties), C is ahead of R, and the guardrail holds | Build "Your Next Bag" properly. |
| **PROMISING** | C is ahead of P and of R, but the primary isn't met | Run **one fresh cohort of 20** under the same sealed spec, with a new sealed salt. It is scored alone with the same rule and **never pooled** with the first. |

**The fresh cohort's outcome is final.** SUCCESS means build. Any other outcome means stop. There is no third cohort.

## Stopping rule

- **No invite goes out before the seal.**
- **The cohort closes** when 20 eligible users have sent their slate, or 21 days after the first invite, whichever
  comes first. Slates sent after the close aren't scored.
- **No interim looks and no early stop** for good or bad results. While the experiment runs, the only numbers
  anyone watches are the count of sent slates and the ratings funnel, which is used for the single day-3 reminder.
  Nobody reads per-user responses or arm results. `mart_rec_arm` is read only by the scorer.
- **The later window** runs from T + 14 to T + 35 days for each scored user. **The scoring run happens once**,
  after the last scored user's T + 35. That is no later than about 57 days after the first invite.
- **Abort** only for harm, a privacy fault or a broken build. Examples: slates that leak the arm, or a consent
  error. An aborted cohort isn't scored. The abort and its reason are recorded here.
- **A scorer or data bug found after the scoring run** is fixed in a new commit. The re-score is reported beside the
  first result, never in place of it.

## Scoring

The inputs are private personal data, so they stay out of git. Keep them in a scratch folder.

```
python eval/recs/score_recs.py responses.csv --exclude exclude.csv --later later.csv \
    --outside outside.csv --funnel funnel.json
```

- `responses.csv`: one row per slate item, with columns `user_id, coffee_id, arm, interest, picked`. This is the
  join of `stg_rec_responses` with `mart_rec_arm`, provided by `mart_rec_eval` (design PR 3).
- `exclude.csv`: `user_id, reason`, written before the scoring run from the exclusions above.
- `later.csv`: `user_id, coffee_id, arm, rating`, for slate coffees rated in the later window.
- `outside.csv`: `user_id, coffee_id, rating, c_score, p_score`, for later ratings outside the slate.
  `p_score` is higher for more popular coffees and is 0 when a coffee is absent from the snapshot.
- `funnel.json`: `{"invited", "rated_five", "answered", "rated_later"}` counts.

The script first checks every frozen file against `SHA256SUMS`, and it exits with code 3 if one has changed. It
prints totals, Wilson 95% intervals, both sign tests and an `OUTCOME:` line. It never prints a user or coffee id.
Exit codes: 0 when scored, 2 for bad input, 3 for a broken seal. It was tested on invented data
(`tests/test_score_recs.py`), including the exact pass and fail boundaries: 15 of 20 against 14 of 20, 13 of 16
against 12 of 16, 10 of 12 against 9 of 12, a "Not for me" gap of exactly +10 points against +12.5 points, and 11
non-tied users.

## The seal

> **PLACEHOLDER: filled in by the seal (design PR 7), after the pilot and before the first invite. Until then, this
> pre-registration is not sealed, and no cohort invite may be sent.**

| Item | Value |
|---|---|
| Sealed on (date, commit) | *PR 7* |
| Pinned catalog release (`config/recs.yml` → `catalog_release`) | *PR 7* |
| Popularity source used (click-outs or the `weeks_listed` fallback) and why | *PR 7* |
| Popularity snapshot file and its sha256 (`config/recs.lock`) | *PR 7* |
| `sha256(cohort salt)` | *PR 7* |
| `experiment_version` the consent records | *PR 7* |
| Pilot changes since this draft (made on pilot aggregates only) | *PR 7* |

`SHA256SUMS` (not yet written: PR 7 creates it) will cover these files, with paths relative to the repo root:
- `eval/recs/README.md`
- `eval/recs/score_recs.py`
- `config/recs.yml`
- `config/recs.lock`
- the popularity snapshot file

To verify, run `sha256sum -c eval/recs/SHA256SUMS` from the repo root. The scorer runs the same check.

## Results

The results don't go in this file, because this file is sealed. After the single scoring run, `eval/recs/RESULTS.md`
will hold the scorer's output, the outcome, the revealed cohort salt, and the date and commit the scoring ran at,
as `eval/match/RESULTS.md` does for its eval.
