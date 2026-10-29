# PR Review: #46 — Origin region context and label fields

RUN_BY_COMMAND: pr-review-local
RUN_BY_SKILL: pr-review
Last reviewed commit: a2c90984c9e70aeef827f6e284eb8b82108cea7c
**Tracker:** github
**Ticket:** TICKET_SKIPPED (reason: github adapter. Review target is PR #46, not a ticket id)
**Review checkout:** `/Users/jasontouleyrou/.cursor/worktrees/pr-review-46-f4eb2a9a/coffee-data-tracker-6cabaf14fc83`
**Base:** `origin/main` (31cd0ef). Branch and main diverge. Three-dot PR diff is 4 files / +134 −12
**PT schema gate:** not triggered (`no_pt_schema_scope_in_diff`)
**Lenses:** none (no Go / scalar-lib footprint. `lens_select.py` also failed on host Python 3.9)

## Verdict

**Approve with nits.** The earlier merge block is gone. That block came from the title-varietal veto change and from un-blanking "Colombia Variety". The two kept fixes behave as claimed against main. This review found no new must-fix code defect on the reduced head.

Prior GitHub comment (2026-10-28) said "don't merge yet". That review targeted the four-rule version. This review targets the reduced head after the author reverted those two rules.

---

## Summary

The PR tightens origin extraction in two ways:

1. Region fallback ignores growing-region names after roaster-place phrasing (`roasted in`, `based in`, `cafe in`, …) and inside business-name suffixes (`Coffee Roasters LLC`, …).
2. Label fields no longer end at a following `Label:` whose name starts with a country word. Example: `Region: Kenya & Ethiopia Roast Level:` keeps both countries and abstains.

Docs update `eval/origin/` for rule-9 accounting, labeller naming, and a spent sealed set. Tests cover the kept fixes. They hold the review probes at "right country or None".

---

## Findings

### Required

None.

### Should-fix

1. **[V] Misleading label-field comment vs `_strip_false_places`** — `src/coffeedata/extract.py` (~220–223)
   - The comment says that in `Origin: Peru, Colombia Varieties: …`, Colombia stays in the field.
   - Verified: `_stated_origins` strips first. `_FALSE_PLACES` blanks `Colombia Varieties`. The field becomes `Peru,` only. Origin is `Peru` (same as main).
   - The real multi-country win is `Origin: Peru, Colombia Process: Washed` (main `Peru` → branch `None`) and `Region: Kenya & Ethiopia Roast Level:` (main `Kenya` → branch `None`).
   - **Fix:** Change the comment example to `Process:` / `Roast Level:`, not `Varieties:`.

2. **[V] `test_a_variety_name_is_not_an_origin` asserts `(truth, None)` while most cases only return `None`** — `tests/test_extract.py`
   - Verified outcomes on head: 6/10 cases return `None` (never `truth`). 3 return the truth country. 1 returns `None` for El Salvador.
   - The assertion still blocks the variety's country (Kenya / Colombia-as-origin). It does not lock today's abstention vs country split.
   - **Fix:** Assert the exact current value per case. Or split "must be None" vs "must be country" tables.

### Nit / residual (logged by author. Re-verified)

3. **[V] Label lookahead can invent a country where main abstained** — `Origin: Huila Peru Lot Size: 10 bags`
   - main → `None`. branch → `Peru`.
   - Mechanism: lookahead refuses to treat `Peru Lot Size:` as the next label because it starts with a country token. So `Peru` enters the field.
   - Author already logged this. No evidence it appears in the five measured product changes. Accept as known residual. Or add a probe that prefers abstention when a region token and a country token share one field without a separator.

4. **[V] Region exclusion does not cover every roaster-place paraphrase** (pre-existing class, still true)
   - `Our roasting studio sits just outside Santa Barbara` → `Honduras` (main and branch).
   - `We ship from our warehouse in Copan weekly` → `Honduras` (main and branch).
   - Out of scope to expand in this PR unless measured.

### FYI

5. **Branch is behind `main`.** `origin/main` has #52 (`threshold-calibration`) and related commits not in this head. Rebase or merge main before ship. Diff vs merge-base still matches the four PR files.

6. **Prior issue comment is stale relative to this head.** Close or reply that the author reverted the two bad rules. Note that this R1 re-reviewed the reduced diff.

---

## Reproductions

Environment: worktree at `a2c90984c9e70aeef827f6e284eb8b82108cea7c`. Commands ran there. Main comparison used a throwaway worktree at `origin/main` (`31cd0ef`).

### Unit tests on PR head

```text
$ uv run pytest -q tests/test_extract.py
94 passed in 0.06s
```

Focused origin tests (region / label / variety / probes): 83 passed.

### Kept fixes vs main

| case | main | branch |
|---|---|---|
| `Small-batch roasted in Santa Barbara…` | Honduras | None |
| `Copan Coffee Roasters LLC…` | Honduras | None |
| `Visit our cafe in Antigua for a cup.` | Guatemala | None |
| `Grown by smallholders in Copán.` | Honduras | Honduras |
| `…moved to Santa Barbara and planted…` | Honduras | Honduras |
| `Region: Kenya & Ethiopia Roast Level: Medium` | Kenya | None |
| `Region: Guatemala & Sumatra BlendAltitude:…` | Guatemala | None |
| `Origin: Peru, Colombia Process: Washed` | Peru | None |
| `Origin: Peru, Colombia Varieties: Caturra` | Peru | Peru |
| `Origin: Huila Peru Lot Size: 10 bags` | None | Peru |

Command shape (branch):

```bash
uv run python -c 'from coffeedata.extract import rules; from tests.test_extract import item; ...'
```

Main: same import against a detached `git worktree` at `origin/main`.

### Strip interaction (finding 1)

```text
raw:      'Origin: Peru, Colombia Varieties: Caturra Roast: Medium'
stripped: 'Origin: Peru,          Varieties: Caturra Roast: Medium'
field after strip: 'Peru,         ' named=['Peru']
stated: ['Peru']  origin: Peru
```

### Variety probe outcomes (finding 2)

```text
got=None         truth='Honduras'   | Finca El Puente Kenya SL-28
got=None         truth='Honduras'   | El Puente - Kenya Variety
got=None         truth='Colombia'   | Kenya SL28 Microlot
got=None         truth='Colombia'   | Kenyan SL-28 | Finca La Riviera
got=None         truth='Colombia'   | Ethiopia Landrace - Finca Inmaculada
got=None         truth='Honduras'   | Kenya Batian
got='Costa Rica' truth='Costa Rica' | Las Lajas Colombia Variety
got='Peru'       truth='Peru'       | Finca Santa Lucia - Colombia Variety
got='Costa Rica' truth='Costa Rica' | Colombia Variety
got=None         truth='El Salvador'| Finca Kilimanjaro - Colombia Varietal
```

---

## End-to-end walk

PR docs point at unit probes and rule-9 hand checks. They do not point at a new CLI path.

| step | result |
|---|---|
| Checkout PR head in isolated worktree | cleared |
| `uv sync` + `uv run pytest -q tests/test_extract.py` | 94 passed |
| Replay PR body product texts (blends + Copan LLC) | all → `None` as claimed |
| Compare same texts on `origin/main` | main still wrong-country on those. Branch abstains |
| `eval/origin/README.md` rule-9 table read | present. Running total 5 |
| Full catalog re-predict vs lake | not run (no `eval_set.parquet` / lake in checkout). Author's 5-product census not re-measured here |
| CI on GitHub (`test`, deploy `check`) | SUCCESS on latest reported runs |

Unchecked PR test-plan items: none beyond the unit tests above. Catalog-scale claim accepted as author-measured. This checkout did not re-prove it.

---

## Adversarial inputs

Changed surface: regex gates in `extract.py` (`_LABEL_FIELD`, `_ROASTER_PLACE`, `_BUSINESS_NAME`). Not a CLI, hook, or installer.

| input class | result |
|---|---|
| Missing / empty body | no origin (existing behavior) |
| Roaster place: `roasted in` / `based out of` / `headquartered in` / `cafe in` | region suppressed |
| Growing place: `grown in Copán` / `planted … Santa Barbara` | region kept |
| Business suffix: `Copan Coffee Roasters LLC`, `Antigua Coffee Company` | region suppressed |
| Near-miss: `just outside Santa Barbara`, `warehouse in Copan` | still Honduras (main same) |
| Label blend: `Kenya & Ethiopia` then `Roast Level:` | None |
| Label multi-country then non-country label: `Peru, Colombia Process:` | None (fix vs main) |
| Label + cultivar blank: `Peru, Colombia Varieties:` | Peru (same as main. Comment wrong) |
| Label starts with country: `Huila Peru Lot Size:` | Peru (regression vs main None) |
| Stated `Region, Country` after cafe: `cafe in Antigua, Guatemala` | Guatemala (region exclusion does not apply to stated origins. Logged) |
| Offline / no `gh` | n/a: library code |
| Adjacent ticket leftovers / `--resume` | n/a: no Cadence gate in this PR |

---

## Contracts

| claim | check |
|---|---|
| CLAUDE.md rule 7: prefer abstain over wrong country | Kept fixes move wrong country → None on measured blends and Copan LLC. Residual `Huila Peru Lot Size` moves None → Peru (logged). |
| CLAUDE.md rule 9: ≤50 products, hand-check | README total is 5. Decision-rule changes normally need the full process. This PR uses the ≤50 path for new region predicates and label termination. Treat as an author decision to waive full sealed re-eval. Mitigation: spent-set note and unit probes. |
| CLAUDE.md rule 6: two labellers | RESULTS names both Claude passes on the sealed set. Human spot-check still open (documented). |
| Code comment "Colombia is in the field" for `Colombia Varieties` | Contradicts `_FALSE_PLACES` strip order. Doc bug, not runtime contract. |
| PR body "logged, not fixed" items | Match reproduced behavior (cafe+country stated. Huila/Peru lookahead). |
| Cadence `core:dry-extension` / `core:standalone-package` | N/A to app extract rules (no new Cadence stage/module). |
| PT / connector schemas | Gate: not in scope. |

---

## Design choices

None for merge-blocking findings. Residuals already have an author-chosen option (log and ship).

If finding 3 were a must-fix, one simple option is abstention. Abstain when a label field has a known one-country region token plus a country token with no comma/`&`/`/`. This review did not build or score that option.

---

## Scope

- Title type matches: origin extraction behavior + eval docs.
- Files: 4 (`extract.py`, `test_extract.py`, `eval/origin/README.md`, `RESULTS.md`).
- Runtime + tests + eval docs for the same rule change. Do not split.
- Diff size is small. Reverts keep blast radius on the two risky rules at zero vs the first #46 head.

---

## Lookback

| finding | failure class |
|---|---|
| Misleading `Colombia Varieties` comment | Contract text written from the new lookahead alone, without re-reading strip order |
| Loose `(truth, None)` asserts | Regression net that encodes review intent, not locked observed outputs |
| `Huila Peru Lot Size` | Negative lookahead coupled to country tokens. Fails closed into a country instead of abstaining |
| Process waiver for decision-rule change under rule 9 | Shortcut intended for case fixes applied to a predicate change |

After any comment or test nits land: re-run `uv run pytest -q tests/test_extract.py`. Re-check the table in Reproductions on the combined tree. Re-read `_stated_origins` order (strip → label → place/region comma).

---

## Test sufficiency

- New tests hit both kept behaviors and the reverted-risk probes.
- No partial-success thresholds.
- Gap: exact expected values for variety probes. Optional probe for `Peru, Colombia Process:` (behavior covered indirectly by Kenya/Ethiopia and Autumn Harvest).
- Catalog-scale 5-product claim not re-run here (no lake in checkout).

---

## Recommendation

Merge after:

1. Fix or delete the `Colombia Varieties` comment example (should-fix #1).
2. Optionally tighten variety asserts (should-fix #2).
3. Rebase onto current `main`.
4. Reply on the old "don't merge yet" issue comment so the thread matches this head.

Do not re-introduce the title-varietal or Colombia-variety un-blanking changes without a fresh sealed set.
