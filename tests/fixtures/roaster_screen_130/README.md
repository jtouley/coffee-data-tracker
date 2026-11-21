# #130's screen test set, as a regression fixture

The sealed test set of #130's discovery screen (`3c8af60:eval/screen/`), byte for byte. It was scored once, on
2026-11-07, so under CLAUDE.md step 5 it is a regression fixture now and never a test set again.
`eval/roaster_screen/README.md` says how the roaster screen's evaluation uses it.

| File | What it is |
|---|---|
| `items_test.json` | The 200 items: id, domain, website, name, city, state, Overture tag |
| `sample_test.json` | Each item's stratum (tag × whether #130's screen found coffee) and the stratum sizes |
| `labels_test_A.json`, `labels_test_B.json` | Two independent labels by Claude subagents (#130 doesn't record which model) |
| `agreed_test.json` | The 163 items where A and B gave the same label and kind |
| `to_settle_test.json`, `settled_test.json` | The 37 disagreements, and a third Claude subagent's answers. No person settled them |
| `pred_main_test.json`, `pred_branch_test.json` | #130's predictions: `main` admitted none; the `own_brand` rule with 5 coffees admitted 20 of the 200, 15 of them among the 181 items with a settled label |
| `SHA256SUMS` | Every other file here: #130's own four lines (the items, the sample and both predictions), then this repo's pins of the five label files and this README |

#130's score on it: precision 89.4% [62.1, 96.3], recall 37.5% [28.4, 62.5], and 2 of 15 admissions a café or a
reseller (`3c8af60:eval/screen/RESULTS.md`).

The evidence #130's screen collected for these items was never in git, and it isn't reused. No test reads these
labels yet. Check the pinned files with `sha256sum -c SHA256SUMS` from this directory.
