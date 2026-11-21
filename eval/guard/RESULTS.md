# Guard evaluation: results (2026-11-07)

Scored once on the sealed test set, after the code and settings were frozen (`dd2eb5d`). Labels: Claude subagents,
two independent labelers plus a tiebreak (`README.md`). Agreement 477/480 = 99.4% [98.2%, 99.8%]; 3 settled.
Unsure labels are left out. Accuracy is weighted by stratum size (D: doubted, held by the guard; N: not doubted,
removed by both arms). Intervals are Wilson 95%.

| Set | Population D / N | Arm | Weighted accuracy | D right | N right | Kept, not coffee | Removed, coffee |
|---|---|---|---|---|---|---|---|
| train | 576 / 1,707 | main | 96.5% | 94/100 [87.5, 97.2] | 37/38 [86.5, 99.5] | 0 | 7 |
| train | | guard | 74.3% | 6/100 [2.8, 12.5] | 37/38 [86.5, 99.5] | 94 | 1 |
| validation | 224 / 621 | main | 100.0% | 100/100 [96.3, 100] | 40/40 [91.2, 100] | 0 | 0 |
| validation | | guard | 73.5% | 0/100 [0.0, 3.7] | 40/40 [91.2, 100] | 100 | 0 |
| **test** | 199 / 430 | **main** | **97.8%** | 146/150 [93.3, 99.0] | 48/49 [89.3, 99.6] | 0 | 5 |
| **test** | | **guard** | **67.8%** | 4/150 [1.0, 6.7] | 48/49 [89.3, 99.6] | 146 | 1 |

**Verdict: the guard fails every part of the ship rule.** Its test accuracy is 30 points below main's. Of the products
it holds, 4 of 150 are coffee (the rule needs more than half), so it would keep about 37 non-coffees public for
every coffee it saves. It stays off, and `rules.EVALUATED` gets no entry for it.

What this says about the classifier: of the products it removed (the 2026-11-04 backfill), about 98% are rightly
removed. `shared_body` doubts are mostly pasted text on genuine non-coffee listings, not a sign of a wrong removal.
The few coffees wrongly removed are better handled one at a time with an `include` decision from the Queue.

These sets can now join the regression fixtures; a new test set is drawn for the next change.

## The guard is cut, and what this measured

The guard was removed from #129 instead of fixed: the mode, `held`, `max_held` and `max_weeks`, the `classifier_guard`
test, and the guard's settings, Queue, Case and readiness parts. `shared_body` stays as a doubt shown on a product's
Case, and `coffeedata.rules.EVALUATED` stays as the gate for any other setting that changes how a rule decides.

**A caveat on what was measured** (#129's final review, MAJOR 1). The guard arm held every public product that a
`verdict_flip removed` flag and a `shared_body` doubt both covered, whatever removed it: a Claude verdict, but also
a rules exclusion (`excluded:<rule>`) or an `unsure` verdict that fell back to the rules. A guard limited to the
removals a verdict made, where the rules alone would keep the product, was never measured. It would need a new
population and a new test set under a new salt. With 4 of 150 held products coffee on this test set, it isn't worth
building now.
