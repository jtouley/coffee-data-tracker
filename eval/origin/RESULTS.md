# Origin extraction: main vs PR #41

This compares `extract.rules()` origin_country on main (579dc1b) and on PR #41 frozen at 9c2c98c ("v2"), on the
live catalog. The protocol is in `README.md`. In short: the products were split by roaster before anything was
read, tuned on train, chosen on validation, and the sealed test set was scored once. The reference is a blind hand
label, because the lake has no Claude extraction.

## Dataset shape (local copy of `jtouley/coffee-data`, read 2026-10-28)

| table | rows | notes |
|---|---:|---|
| snapshots | 141,347 | One row per product per run. Runs: 2026-10-26 (100,311), 10-27 (1,756), 10-28 (39,280). 103,427 distinct products from 1,890 roasters. 91 products changed `body_hash` between runs. 16,986 rows have an empty `body_hash` (no description); every non-empty hash resolves in `bodies`. |
| bodies | 83,674 | Unique by `body_hash`. Text length: median 396 characters, max 66,907. |
| enrichments | 48,781 | **`method` = `rules` only.** 24,652 rows have an origin; 43,233 distinct body hashes. No `llm` rows. |
| verdicts | 0 | No Claude classifier verdicts, so `stg_products` uses the rules alone. |
| marts/dim_coffee | 44,293 | The current classifier's coffees, 44,190 of them listed. `enrichment_method` = `rules` for all; 23,262 have an origin. |

There is no Claude reference for origin. main's rules re-run on the eval set give exactly 23,262 origins,
matching `dim_coffee`, so the harness reproduces production.

## What changes on the whole catalog (44,293 coffees)

| | count |
|---|---:|
| unchanged | 42,838 |
| country → None (branch abstains; `needs_llm` sends these to Claude) | 1,207 |
| None → country (new: a one-country region with no country named anywhere) | 207 |
| country → different country | 41 |
| products with an origin | 23,262 → 22,262 |

- **Top changes:** Brazil→None 230, Colombia→None 215, Guatemala→None 121, Ethiopia→None 108, Indonesia→None 91,
  None→Colombia 76, Mexico→None 65, Honduras→None 58, Costa Rica→None 54, Peru→None 48, None→Ethiopia 43.
- **None → country by country:** Colombia 76, Ethiopia 43, Mexico 25, Guatemala 14, Honduras 14, Kenya 6, Brazil 6,
  Costa Rica 6.
- **By split** (unchanged / country→None / None→country / country→other):
  - train: 27,175 / 709 / 103 / 14
  - validation: 7,533 / 260 / 66 / 20
  - test: 8,130 / 238 / 38 / 7

**Examples (train only):**

- None → country:
  - "Huila Decaffeinated" ("producers in San Agustín, Huila") → Colombia
  - "Sierra Mazateca, Oaxaca" → Mexico
  - "SIPRO" ("…Huehuetenango…") → Guatemala
- country → None, mostly blends main gave their first-named country:
  - "Summit Sunrise" ("Origin: Honduras, Peru, Sumatra"), was Honduras
  - "Numinous Espresso #5" ("50% Ethiopia … 50% Nicaragua"), was Ethiopia
- country → other:
  - "NAHUN FERNANDEZ" ("Location: Las Flores, Honduras"): Indonesia → Honduras
  - "Macaron" ("Las Flores in Pitalito, Huila"): Indonesia → Colombia
  - On train, the branch's remaining country→other changes are mostly blends where both are wrong (e.g. "Mayday",
    "Lionheart": one blend component for another).

## Agreement with the hand labels

The whole-set estimate weights each stratum by its size (±: 95%). The paired estimate is branch minus main on the
same products, so unchanged products add nothing, and it is much tighter.

| set | labelled | main agree | branch agree | main wrong-country | branch wrong-country | paired agree | paired wrong-country |
|---|---:|---:|---:|---:|---:|---:|---:|
| train | 89 | 93.2% ± 7.5 | 96.0% ± 7.5 | 6.5% | 3.9% | **+2.8% ± 0.2** | **−2.5% ± <0.1** |
| validation | 111 | 84.4% ± 12.1 | 88.0% ± 12.1 | 14.8% | 11.6% | **+3.6% ± 0.7** | **−3.2% ± 0.3** |
| **test (sealed, scored once)** | 82 | 89.3% ± 10.3 | 91.9% ± 10.3 | 10.3% | 7.7% | **+2.6% ± 0.7** | **−2.6% ± 0.3** |

**Test, per change type** (Wilson 95%):

| stratum (size) | main agree | branch agree | main wrong-country | branch wrong-country |
|---|---|---|---|---|
| country → None (238) | 3/25, 12% [4, 30] | 22/25, 88% [70, 96] | 22/25 | 0/25 |
| country → other (7) | 0/7, 0% [0, 35] | 7/7, 100% [65, 100] | 7/7 | 0/7 |
| None → country (38) | 1/25, 4% [1, 20] | 24/25, 96% [81, 99] | 0/25 | 1/25 |
| unchanged (8,130) | 23/25, 92% [75, 98] | 23/25, 92% [75, 98] | 2/25 | 2/25 |

**Verdict on the acceptance bar:**

- The branch agrees with the reference at least as often as main, overall and in every change type.
- Its wrong-country rate is lower overall.
- The one new kind of wrong-country error is in None → country, where a region name gives a country.
  - test: 1 of 25 (about 1.5 of the set's 38). It is not a blend: a roaster's service page names
    "Copan Coffee Roasters LLC", and the Copán region table read the company name as Honduras. #46 stops a region
    right before a roaster's name ("Roasters", "LLC") or right after "roasted in" from counting as a region-only origin. That rule was prompted by this test example,
    which is acceptable only because the test set is now retired (see README.md).
  - train: 2 of 25
  - Main's errors are much larger: 22 of 25 in country → None and 7 of 7 in country → other on test.

## How the rules were chosen

- **v1 (15c7f6e):** the context rank table. Dropped after review, for misreading ordinary listings. It isn't scored
  here.
- **Fixes from train examples:**
  - A labelled field ("Origin: Brazil/Honduras", "Origin: Blend (Brazil, Colombia and Sumatra)") now contributes every
    country in it. Before, only the one after the label counted.
  - "Region, Country" only counts when the region belongs to that country, so "Chiapas, Brazil" is a list, not a
    location.
  - These fixes cut train country→other from 24 to 14.
- **Validation choice:** between v2 (a stated origin wins over other countries in the description) and v2b (every
  country must agree, otherwise None).
  - Both scored the same overall: agreement +3.6 / +3.5 points, wrong-country −3.2 / −3.3 points, paired.
  - In the 32 products where they differ, v2b abstained on correct origins: 24% agreement against main's 60% and v2's
    68%. That fails "at least as good as main per change type".
  - v2 was chosen and frozen at 9c2c98c.

## Probe set (unit tests, not evaluation)

`tests/test_extract.py::test_origin_probes` covers 70 listings: the reviewers' cases, earlier reproductions and the
El Puente lots.

- On the branch: 48 right, 21 None and 1 wrong, the same wrong answer as main ("This Burundian lot is
  double-fermented Kenya-style", Kenya).
- No case gets a different wrong country.
- Main gets 34 of the 70 right.
- The El Puente SL-28 lot gives Honduras ("Marcala, Honduras"). Without that country it gives None, also when the
  title says "Kenya SL-28" (`test_a_variety_name_is_not_an_origin`).

## Limits

- Labellers:
  - First label of every set: Claude Opus 5.5 (`claude-opus-5-5`), the agent that wrote the rules, blind to
    predictions.
  - Sealed test: labelled a second time, independently and blind, by Claude in the PR review
    (https://github.com/jtouley/coffee-data-tracker/pull/41#issuecomment-6482198105). The two agreed on 82 of 82
    (Wilson 95% [95.5%, 100%]), so no third labeller was needed (CLAUDE.md rule 6). Scored against the second labels,
    the result is the same.
  - Both labellers are Claude, so they may share blind spots. A person's spot-check is still to come.
  - Train and validation were labelled once.
- With 25 labels per stratum, the per-stratum intervals are wide; the paired differences are the decisive numbers.
- The cost of abstaining is 1,000 fewer rules origins (1,207 out, 207 in). With Claude enrichment enabled,
  `needs_llm()` sends those products to Claude; without it they stay empty, where main had a guess that the labels
  show was wrong about 88% of the time (test, country → None).
