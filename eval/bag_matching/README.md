# Bag matching: evaluation

This eval measures how often the matcher (`shots.match_coffee` for typed text, `shots.match_bag` for a read
label) picks the coffee a person's bag is saved under. The cases are the lake's real `bags` rows that have a
`coffee_id`. It follows `CLAUDE.md`.

- Split: `sha256("bag-matching-eval-2026-10-28" + roaster of the bag's coffee)` read as a fraction. Below 0.6 is
  train, below 0.8 is validation, and the rest is test.
- `python score.py <lake> <dim_coffee export> [--final]` prints counts and per-set match rates with Wilson
  intervals. A set with fewer than 30 inputs isn't scored. Test is scored only with `--final`.
- The reference is the saved `coffee_id`. That is what the person kept, but it isn't independently labeled. A bag
  saved under a wrong lot counts as truth until it's edited.

## Status (snapshot df57f62, 2026-10-28)

There are 2 bags with a `coffee_id`: 1 in train, 0 in validation and 1 in test. That makes 3 inputs: 2 typed
texts and 1 read label. This is too few to score or to choose between matchers.

All matcher versions score the same on these inputs, using the snapshot's own export: 1 of 2 typed texts and 0
of 1 read labels. That covers `main` (b309ad0), a1733aa, 6cd500b, and this change (a wholesale candidate that
outscores the retail pick makes the match unclear). Neither bag involves a wholesale listing, so the change can't show
on them.

With a rebuilt export that has the same rows in another order, the versions measured then (`main` at 7ecc1a6,
a1733aa, and this branch before its later merges) all get 0 of 2 typed texts. The 300-candidate prefilter breaks ties by row order, which is worth fixing
on its own.

To be meaningful, this needs:
- at least 30 inputs per set, and about 150 bags overall, from many roasters;
- each bag's photo read or typed text;
- a `coffee_id` the person confirmed;
- some wholesale bags among them, so retail-first matching can be measured against matching over the whole catalog.

Until then, the matching rules rest on the unit tests in `tests/test_bags.py`, which reproduce the reviewed cases.
