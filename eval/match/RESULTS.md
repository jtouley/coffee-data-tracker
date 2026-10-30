# Typed-text matching: main vs #57

`shots.match_coffee` on 1,000 coffees per set, two queries each (title, roaster+title). The definitions are in
`README.md`. Each cell gives the count of 1,000 queries, then the Wilson 95% interval.

| set | form | main right | #57 right | main abstain | #57 abstain | main conf. wrong | #57 conf. wrong |
|---|---|---|---|---|---|---|---|
| train | title | 386 [35.6, 41.7]% | **778** [75.1, 80.3]% | 557 | 220 | 57 [4.4, 7.3]% | **2** [0.1, 0.7]% |
| train | roaster+title | 29 [2.0, 4.1]% | **843** [81.9, 86.4]% | 957 | 157 | 14 [0.8, 2.3]% | **0** [0.0, 0.4]% |
| validation | title | 384 [35.4, 41.5]% | **803** [77.7, 82.6]% | 558 | 194 | 58 [4.5, 7.4]% | **3** [0.1, 0.9]% |
| validation | roaster+title | 24 [1.6, 3.5]% | **829** [80.4, 85.1]% | 960 | 171 | 16 [1.0, 2.6]% | **0** [0.0, 0.4]% |
| **test** | title | 359 [33.0, 38.9]% | **787** [76.1, 81.1]% | 583 | 209 | 58 [4.5, 7.4]% | **4** [0.2, 1.0]% |
| **test** | roaster+title | 24 [1.6, 3.5]% | **843** [81.9, 86.4]% | 957 | 157 | 19 [1.2, 2.9]% | **0** [0.0, 0.4]% |

Test, both forms (2,000 queries): right 19.1% [17.5, 20.9] for main and 81.5% [79.7, 83.1] for #57; confidently
wrong 3.9% [3.1, 4.8] for main and 0.2% [0.1, 0.5] for #57. Outputs changed on 1,293 of the 2,000 test queries.

**Ship rule: met.** On test, #57's right is higher than main's on both forms, with intervals that don't overlap,
and its confidently wrong is lower.

Notes:
- On train, #57's 2 confidently wrong answers were both ambiguous title-only queries: several roasters list the
  same title, and #57 picked one (its own coffee), which counts as a guess. Main roaster+title does worse than
  main title because the roaster's words add more partial_token_set_ratio ties: that's the crowding #57 fixes.
- Speed: #57 runs two `process.extract` scans and scores up to 600 candidates instead of 300.
  - In a single-process run of 600 calls on the same catalog, it took 0.32 s per call against main's 0.20 s.
  - The test predictions, run on 4 workers with nothing else running, took 0.262 s against 0.158 s, about +65%.
  - The train and validation runs' s/call figures were taken while other jobs were running, so they aren't
    comparable.
