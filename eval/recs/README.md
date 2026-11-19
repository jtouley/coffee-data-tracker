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

- **C, content.** Nearest neighbours on catalog attributes. The rule is frozen here and in `config/recs.yml` →
  `content`; `dbt/models/recs/` (design PR 3) must implement exactly this, with unit tests on invented coffees.
  - **Features.** All values are `lower(trim(x))` from the pinned release's `dim_coffee`; an empty value is absent.
    Five blocks:
    - origin: `country:<origin_country_code>` at weight 1, and `region:<code>:<origin_region>` at weight 0.5 (only
      with a country);
    - process: `process:<process>` at weight 1;
    - roast: `roast:<roast_level>` at weight 1;
    - varietals: `variety:<v>` for each distinct varietal, at weight idf;
    - flavor: `flavor:<c>` for each distinct `flavor_categories` entry, at weight idf.
  - **IDF** = ln((1 + N) / (1 + df)) + 1. The corpus is every coffee in the pinned release's `dim_coffee` with
    `is_coffee` and not `is_wholesale`, in stock or not, so past-offering seed coffees are in it. N is its size,
    and df is the number of its coffees that carry the feature.
  - **Normalisation.** The weights above (the region's 0.5 included) are set first; then each present block is
    L2-normalised to norm 1. An absent block contributes nothing, so a coffee without notes isn't penalised.
  - **Profile** = Σ over the seed coffees of (rating − the mean of the user's five ratings) × vector. If all five
    ratings are equal, or the centred profile is the zero vector (norm < 1e-12, as when two identically described
    coffees are rated 5 and 1 and the rest at the mean), the profile is Σ rating × vector instead. That is never
    zero, because ratings are ≥ 1 and vectors are non-negative. A seed coffee absent from the pinned release has no
    vector but still counts in the mean.
  - **Score** = profile · v / (‖profile‖ ‖v‖), rounded to 12 decimals so every engine gives the same order. Ties go
    to the lower `coffee_id`.
- **P, popular.** The highest-ranked eligible coffees in the frozen popularity snapshot.
  - **Source (owner's default):** site click-outs over the trailing 8 weeks, frozen at the seal. This applies once
    the analytics design is live and the window holds at least 300 click-outs. Coffees are ranked by click-outs,
    descending.
  - **Fallback, used if click-outs aren't available at the seal:** the **longest-listed in-stock coffees** in the
    pinned release, ranked by `dim_coffee.weeks_listed` descending, then `first_seen` ascending, then `coffee_id`
    ascending. These are the roasters' staple coffees.
  - The seal records which source was used and the click-out count. Either way, the ranking is one fixed file, and
    its sha256 is pinned.
  - **Each user's P list is drawn, not fixed.** Take the user's eligible coffees in popularity order, keep the top
    **K = 20**, and order those 20 by `h(salt|user_id|P|coffee_id)` ascending. The rest follow in popularity order,
    in case the draft runs through the 20.
  - **What this does to the comparison.** Without the draw, every user's P pair would be nearly the same two staple
    coffees. The primary would then measure C against those two specific coffees, and participants comparing
    slates, or anyone reading the public catalog, could pick out P. With the draw, P is "a typical coffee from the
    20 most popular": a slightly weaker baseline than the very top two, but the one the question asks about, and
    different for each user. One limit remains. Someone who computes the top 20 from public data can tell that a
    card is among them, though C or R cards can be too.
- **R, random.** A uniform draw from the eligible coffees, seeded per user: coffees are ordered by
  `h(salt|user_id|R|coffee_id)` ascending.

**Eligible coffees** (per user, all from the pinned catalog release, `config/recs.yml` → `eligibility`):
- `is_coffee`, retail (not `is_wholesale`), and `in_stock` in the pinned release (never live stock);
- at least two of the five content blocks present;
- not one of the user's five rated coffees;
- not decaf, unless the user rated a decaf;
- at most one coffee per roaster in a slate.

## Assignment: three-team draft (multileaving) by salted hash

`h(x)` is the lowercase hex SHA-256 of the UTF-8 string `x`. Hashes are compared as strings, so ascending string
order is the same as ascending numeric order. `|` is a literal separator.

1. Each arm ranks the user's eligible coffees, as described above.
2. **Draft:** there are two rounds, `r = 1, 2`. In round `r`, the three arms take turns in ascending order of
   `h(salt|user_id|draft|r|arm)`. On its turn, an arm takes the first coffee on its list that isn't already in the
   slate and whose roaster isn't already in the slate. That coffee belongs to that arm, so a coffee wanted by two
   arms goes to whichever drafts first, and the other arm takes its next choice.
3. **Positions:** the six drafted coffees are shown in ascending order of `h(salt|user_id|slot|coffee_id)`. The
   draft order says nothing about where a card appears.

This is team-draft interleaving extended to three teams, a form of multileaving.

The slate is a pure function of the raw seed ratings, the pinned release, the pinned popularity file and the salt.
There is no wall-clock time and no live stock, so a rebuild gives the same slate (the owner's idempotency rule).

**The slate record.** The first time a user's slate is built, the nightly job (design PR 5) also writes it with its
arms (`user_id, slate_id, coffee_id, arm`) to an append-only private table, one file per slate, never rewritten.
The scorer takes arms from that record, not from a rebuild, so a model change after a slate was shown can't relabel
its cards. The rebuilt `mart_rec_arm` must equal the record, which is checked when the scoring inputs are made.

### The salt: sealed by hash, revealed at the end

The repo is public. With a committed salt and the public code, anyone who knew a participant's `user_id` could work
out which card came from which arm. The consent line promises participants that they aren't told this until the
test ends. So:

- **Cohort salt.** At the seal, the owner generates 32 random bytes, written as 64 hex characters, for example with
  Python's `secrets.token_hex(32)`. The salt is stored only as a private secret for the nightly rec build, never in
  git, logs or any public table. **Only `sha256(salt)` is committed**, in the seal table below: the hash of the
  64-character lowercase hex string as UTF-8, the same `h()` as above. At 256 bits, the salt can't be recovered
  from its hash.
- **The build uses the sealed salt or nothing.** The cohort build refuses to run without the cohort salt secret and
  never falls back to the pilot salt. Each build records `h(salt)` of the salt it used (the hash, never the salt),
  and the scoring inputs are made only if that matches the sealed hash.
- **Revealed at the end.** After the scoring run, the salt is written into `RESULTS.md`. Anyone can check that its hash
  matches the sealed one and rebuild `mart_rec_arm` from it.
- **Pilot salt.** This salt is committed, because pilot users are unblinded and never scored:
  `coffeedata-recs-pilot-2026-11`.

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
facts. A user is always dropped whole, never item by item. Each has a fixed code. The scorer accepts no other code,
refuses free text, and prints only the count for each code, never an id.

| Code | Exclusion | Applied by |
|---|---|---|
| `insider` | The owner, the pilot users and contributors (see above) | the exclusions file |
| `duplicate_account` | A second account belonging to the same person; the account whose consent came first is kept | the exclusions file |
| `withdrawn` | Consent withdrawn before the scoring run (erased users are gone from every table and counted in the funnel) | the exclusions file |
| `not_sent` | The slate wasn't sent ("Send answers") on or before the cohort's close. Answers saved as you go don't count | the scorer, from `sent` |
| `malformed_slate` | Not six coffees, not two per arm, a repeated coffee, an unanswered item, or not exactly one "buy next" pick: a build or app fault | the scorer |

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
| Later rating | 1–5 ratings of slate coffees the user drank, logged in **the later window** (the latest per user and coffee). For each arm: n, mean, and the share ≥ 4 with a Wilson 95% interval. | secondary, descriptive |
| Ranking of later coffees | Covers coffees the user rates in the later window that weren't in the slate or the seed. For each user with at least two differently rated coffees: the share of differently rated pairs that C's score orders correctly, against the share that P's popularity orders correctly (ties count as half; a coffee absent from the snapshot ranks last). Reports the users for whom C does better, with a Wilson 95% interval over non-tied users. | secondary, descriptive |
| Completion | invited → five ratings → slate sent → a later rating | funnel, for planning the next cohort |

**The later window** is days 14 to 35 after T, both included, for every later measure. The scorer applies it
itself from each rating's `day`.

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

**Ties lower the power.** A user whose C and P interest are equal drops out of the sign test. With answers of 0, 1
or 2 on two items per arm, a tie rate of about **25%** is expected (the reviewer's estimate for independent answers;
the real rate isn't known yet). The cohort still closes at 20 sent slates and the threshold doesn't change, so the
power that matters is at the n left after ties:

| Ties among 20 sent slates | n left | must favour C | 60% | 70% | 75% | 80% | 85% |
|---|---|---|---|---|---|---|---|
| 0% | 20 | 15 | 13% | 42% | 62% | 80% | 93% |
| **25% (expected)** | **15** | **12** | 9% | 30% | 46% | **65%** | 82% |
| 40% | 12 | 10 | 8% | 25% | 39% | 56% | 74% |

So the realistic power to confirm a four-in-five preference is **about 65%, not 80%**.

Plainly: **20 users can confirm that about four in five people prefer C. They can't detect a 60/40 or even a 70/30
preference,** so a miss is weak evidence against C. 15 of 20 has a Wilson 95% interval of 53–89%. The later ratings
(about 10–30 bags) are descriptive only: 7 of 20 rated ≥ 4 has an interval of 18–57%.

## Outcomes (decided before any data)

The outcomes are checked in this order. `score_recs.outcome` implements this table, and its tests cover each
boundary.

| Outcome | When | What happens |
|---|---|---|
| **INCONCLUSIVE, too few** | Fewer than **12** non-tied C-vs-P users | No verdict. Not built. |
| **FAIL** | C ≤ P in direction (k ≤ n − k), **or** C ≤ R in direction | Stop recommendation work and return to insights (the product order in NEXT-STEPS). |
| **INCONCLUSIVE, guardrail** | C is ahead of P and of R, but C's "Not for me" rate is more than 10 points above P's, **whether or not the primary is met** | Not built. |
| **SUCCESS** | The primary is met (≥ 15 of 20, or the threshold above for the n left after ties), C is ahead of R, and the guardrail holds | Build "Your Next Bag" properly. |
| **PROMISING** | C is ahead of P and of R, and the guardrail holds, but the primary isn't met | Not built. Reported as promising. |

The "Not for me" limit is checked on every result that isn't a FAIL, and it is reported on a FAIL.

**One sealed run only. There are no reruns.** No outcome leads to a second cohort under this pre-registration, and
no cohorts are ever pooled. Anything after this run, for any outcome, is a new experiment with its own
pre-registration and its own seal. This replaces the design's "one fresh cohort" for a promising result.

## Stopping rule

- **No invite goes out before the seal.**
- **The cohort closes** when 20 eligible users have sent their slate, or 21 days after the first invite, whichever
  comes first. Slates sent after the close aren't scored.
- **No interim looks and no early stop** for good or bad results. While the experiment runs, the only numbers
  anyone watches are the count of sent slates and the ratings funnel, which is used for the single day-3 reminder.
  Nobody reads per-user responses or arm results. `mart_rec_arm` is read only by the scorer.
- **The later window** is T + 14 to T + 35 days for each scored user. **The scoring run happens once**,
  after the last scored user's T + 35. That is no later than about 57 days after the first invite.
- **Abort** only for harm, a privacy fault or a broken build. Examples: slates that leak the arm, or a consent
  error. An aborted cohort isn't scored. The abort and its reason are recorded here.
- **A scorer or data bug found after the scoring run** is fixed in a new commit. The re-score is reported beside the
  first result, never in place of it.

## Scoring

The inputs are private personal data, so they stay out of git. Keep them in a scratch folder.

```
python eval/recs/score_recs.py slates.csv responses.csv --exclude exclude.csv --later later.csv \
    --outside outside.csv --funnel funnel.json
```

The input contract (design PR 3 makes `mart_rec_eval` one row per slate item, not per-user aggregates, so it can
produce `responses.csv`):

| File | Columns | Rows |
|---|---|---|
| `slates.csv` | `user_id, slate_id, coffee_id, arm` | The slate record as first built (see **Assignment**): six rows per slate, one slate per user. |
| `responses.csv` | `user_id, slate_id, coffee_id, interest, picked, sent` | One row per answered item. `interest` is 0, 1 or 2, or blank if unanswered. `picked` is 0 or 1. `sent` is 1 if the slate was sent on or before the close, else 0. A missing item has no row. |
| `exclude.csv` | `user_id, reason` | One row per excluded user, using only the codes in **Exclusions**. Written before the scoring run. |
| `later.csv` | `user_id, coffee_id, rating, day` | Every rating (1–5) of a slate coffee after T. `day` is whole days after T. |
| `outside.csv` | `user_id, coffee_id, rating, day, c_score, p_score` | Every later rating of a coffee outside the slate and the seed, with C's frozen score and the popularity score (higher is more popular, 0 when absent from the snapshot). |
| `funnel.json` | `invited, rated_five, answered, rated_later` | Counts. |

**What the scorer checks and refuses** (exit 2):
- a response for a coffee or slate not in the slate record;
- an arm, interest, `picked`, `sent`, rating or day outside its values;
- a non-finite or non-numeric score;
- a later rating of a coffee outside the user's slate, or an outside rating of one inside it;
- two ratings of one coffee by one user on the same day;
- an exclusion code that isn't listed;
- a missing file or malformed funnel.

It reads and checks every input before it prints anything. The arm always comes from the slate record. It applies
the later window and the latest rating per user and coffee itself.

**The seal is required** (exit 3 otherwise). The scorer needs `SHA256SUMS` present, every line well formed and
matching, `config/recs.yml`'s `catalog_release` and `popular.source_chosen` filled in, and the placeholder below
gone. `--unsealed-dry-run` skips this for the pilot or a rehearsal. It labels its output as a dry run and never
prints an `OUTCOME:` line.

The scorer prints totals, Wilson 95% intervals, both sign tests and an `OUTCOME:` line, and never a user or coffee
id. Exit codes: 0 when scored, 2 for bad input, 3 when not sealed or the seal is broken. It is tested on invented
data (`tests/test_score_recs.py`), including the exact pass and fail boundaries: 15 of 20 against 14 of 20, 13 of
16 against 12 of 16, 10 of 12 against 9 of 12, a "Not for me" gap of exactly +10 points against +12.5 points, and
11 non-tied users.

## The seal

> **PLACEHOLDER: filled in by the seal (design PR 7), after the pilot and before the first invite. Until then, this
> pre-registration is not sealed, and no cohort invite may be sent.**

| Item | Value |
|---|---|
| Sealed on (date, commit) | *PR 7* |
| Pinned catalog release (`config/recs.yml` → `catalog_release`) | *PR 7* |
| Popularity source used (click-outs or the `weeks_listed` fallback), the click-out count, and why | *PR 7* |
| Popularity snapshot file and its sha256 (`config/recs.lock`) | *PR 7* |
| `sha256(cohort salt)` (and, at scoring, the hash every cohort build recorded) | *PR 7* |
| `experiment_version` the consent records | *PR 7* |
| Pilot changes since this draft (made on pilot aggregates only) | *PR 7* |

`SHA256SUMS` (not yet written: PR 7 creates it) covers every file that decides an arm, a slate or a score, with
paths relative to the repo root. PR 7 writes one line for each file that exists at the seal:
- the spec and scorer: `eval/recs/README.md`, `eval/recs/score_recs.py`;
- the settings: `config/recs.yml`, and `config/recs.lock` (the release id, the salt hash and the popularity
  file's sha256);
- the popularity ranking: `config/recs_popularity.csv` for the `weeks_listed` fallback, which is public data.
  Click-out counts are private analytics, so with that source the file stays out of git, and only its sha256 in
  `config/recs.lock` is sealed;
- **every file under `dbt/models/recs/`**, where features, eligibility, profiles, the draft and arms are built.
  The design expects `stg_ratings.sql`, `stg_rec_responses.sql`, `int_coffee_features.sql`, `int_rec_profile.sql`,
  `int_rec_popularity.sql`, `mart_rec_slate.sql`, `mart_rec_arm.sql`, `mart_rec_eval.sql` and their schema YAML.
  PR 7 lists the files as they are, all of them;
- every macro or seed those models reference, by path;
- the nightly rec build workflow and the slate-record writer (design PR 5), by path.

The rec models read only the pinned release's stored `dim_coffee`, never a rebuild, so changes upstream of it don't
change a slate. A change to any sealed file breaks the seal, and the scorer refuses to run.

To verify, run `sha256sum -c eval/recs/SHA256SUMS` from the repo root. The scorer runs the same check.

## Results

The results don't go in this file, because this file is sealed. After the single scoring run, `eval/recs/RESULTS.md`
will hold the scorer's output, the outcome, the revealed cohort salt, and the date and commit the scoring ran at,
as `eval/match/RESULTS.md` does for its eval.
