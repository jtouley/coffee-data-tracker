# Run quality: which batch counts for a roaster-week

The field is the batch `stg_run_walk` counts for each roaster-week (the week's newest successful batch, unless an
override pins one), and so `stg_run_quality`'s suspect flag, `stg_runs`, `is_current` and prices in the release.
Changes that alter which batches exist or count are measured here.

**Running total (CLAUDE.md step 9): 346 products** (an upper bound), since this field has had no full evaluation.
Over 50, so the full process (steps 2 to 7) is still owed. Add each later change's count here.

## #93: post-crawl repulls (crawl-dq)

On `main` a roaster with an ok fetch is never fetched again that week. With #93, a crawl pass after the post-crawl
check fetches the check's `empty` and `suspect` roasters again (not `failed` ones: every pass asks those anyway, as on
`main`), and a successful re-fetch becomes the batch that counts. Only those roasters' products can change.

A real re-fetch can't be simulated offline, so the count is the upper bound: every distinct product the repull-listed
roasters had in the 35 days up to the run (snapshots). New products a re-fetch would find are not in it.

**Replay** (`replay_repulls.py`, the check's dbt step and `crawldq.judge` with `attempted_only`): a local copy of
the lake, read only, holding runs 2026-10-26 to 2026-11-02, cut to the run being judged (later `run_date`
partitions removed, because `suspect_runs_need_review` reads the lake's latest week). `HF_HUB_OFFLINE=1`, no token.

| Run | Systemic | Listed | Held back | Repulled (empty / suspect) | Products, upper bound |
|---|---|---|---|---|---|
| 2026-10-26 (weekly) | none | 67, all `failed` | 0 | 0 | 0 |
| 2026-10-27 (retry run) | none | 0 | 0 | 0 | 0 |
| 2026-10-28 (Shopify 429s) | `connector` shopify | 46 | 912 | 2 | 56 |
| 2026-11-02 (weekly) | none | 63 | 0 | 11 | 346 |

2026-10-26 is the lake copy's first run: with no earlier weeks the suspect rule can't fire, so it understates a
typical week. 2026-11-02 is the only weekly run with history, and the number recorded above: 11 roasters
(5 empty, 3 suspect, 3 both), 346 products. The roasters' counts at the suspect fetch against their baseline: 0 of
33, 8 of 52, 9 of 22, 0 of 126, 0 of 20, 21 of 55.

Reproduce, from the repo root, with the lake copy cut to `<run_date>`:

```sh
HF_HUB_OFFLINE=1 python eval/run_quality/replay_repulls.py <lake copy> <out dir> <run_date>
```
