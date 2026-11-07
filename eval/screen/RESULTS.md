# Discovery screen: results (2026-11-07)

Rule chosen on validation aggregates (`screen_eval.py choose`): **`own_brand`, MIN_COFFEES = 5** (highest F0.5,
0.810). Frozen in `src/coffeedata/screen.py`, then `main`'s and the change's test predictions were written, pinned in
`SHA256SUMS`, and scored once.

| Set | Arm | Labeled | Admitted | Precision | Recall | Confidently wrong (cafe/reseller admitted) | F0.5 |
|---|---|---|---|---|---|---|---|
| validation | own_brand, 5 | 191 | 25 | 94.4% [75.0, 97.8] | 51.6% [43.4, 72.9] | 1 = 4.6% [0.7, 19.5] | 0.810 |
| **test** | main | 181 | 0 | n/a | 0.0% [0.0, 11.7] | 0 | 0.000 |
| **test** | **own_brand, 5** | 181 | 15 | **89.4% [62.1, 96.3]** | **37.5% [28.4, 62.5]** | **2 = 10.6% [3.7, 37.9]** | 0.700 |

Stratum-weighted; Wilson 95% intervals on the labeled counts (an approximation for a weighted ratio, so a point
estimate can sit near or outside its interval). Unsure labels are left out.

Reading: about 9 in 10 admissions are real roasters selling their own coffee online, and the screen finds roughly a
third to a half of the roasters in the candidate pool. `main` finds none of them. Confidently wrong admissions rise
from 0 to about 1 in 10 admissions (2 of 15 on test; the interval is wide).

Whether that rise is acceptable is the owner's call; admission stays off (`--admit`) until then.
