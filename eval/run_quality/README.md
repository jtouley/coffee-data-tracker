# Run quality: which batch counts for a roaster-week

The field is the batch `stg_run_walk` counts for each roaster-week (the week's newest successful batch, unless an
override pins one), and so `stg_run_quality`'s suspect flag, `stg_runs`, `is_current` and prices in the release.
Changes that alter which batches exist or count are measured here.

**Running total (CLAUDE.md step 9): 0 products** since this field's last full evaluation (it has had none).
#93 (the fetch-health check) ships with re-fetch off (`COFFEEDATA_DQ_REFETCH` unset), and off no crawl pass reads
its list, so the week's output is `main`'s: 0 changed products. Its mapping of `main`'s partial, quarantined and
challenged fetches only changes what the check reports, never a batch: still 0. Add each later change's count here.

**Before re-fetch is turned on:** it would change up to 346 products in a weekly run (below), over 50, and it changes
how a rule decides (which batch counts), so CLAUDE.md's full process (steps 2 to 7) is required first: a held-out
split grouped by roaster, labels made against a written definition of a good batch, and `main` against re-fetch on
the sealed test set with Wilson intervals. Turning it on without that is not allowed; when it is turned on, add its
measured count to the running total above.

## #93: the fetch-health check's re-fetches (fetch-health), if re-fetch were on

The check judges fetches (failed, empty, or far fewer products than usual), not the data in them. What follows
measures re-fetch on; #93 ships it off. On `main` a roaster with an ok fetch is never fetched again that week. With
re-fetch on, a crawl pass after the check fetches its `empty` and `suspect` roasters again (not `failed` ones: every pass asks those anyway, as on `main`), and a successful re-fetch becomes the
batch that counts. Only those roasters' products can change.

A real re-fetch can't be simulated offline, so the count is the upper bound: every distinct product the listed
roasters had in the 35 days up to the run (snapshots). New products a re-fetch would find are not in it.

**Replay** (`replay_repulls.py`, the check's dbt step and `fetchhealth.judge` with `attempted_only`): a local copy of
the lake, read only, holding runs 2026-10-26 to 2026-11-02, cut to the run being judged (later `run_date`
partitions removed, because `suspect_runs_need_review` reads the lake's latest week). `HF_HUB_OFFLINE=1`, no token.

| Run | Systemic | Listed | Held back | Re-fetched (empty / suspect) | Products, upper bound |
|---|---|---|---|---|---|
| 2026-10-26 (weekly) | none | 67, all `failed` | 0 | 0 | 0 |
| 2026-10-27 (retry run) | none | 0 | 0 | 0 | 0 |
| 2026-10-28 (Shopify 429s) | `connector` shopify | 46 | 912 | 2 | 56 |
| 2026-11-02 (weekly) | none | 63 | 0 | 11 | 346 |

2026-10-26 is the lake copy's first run: with no earlier weeks the suspect rule can't fire, so it understates a
typical week. 2026-11-02 is the only weekly run with history, and the number cited above for re-fetch on: 11 roasters
(5 empty, 3 suspect, 3 both), 346 products. The roasters' counts at the suspect fetch against their baseline: 0 of
33, 8 of 52, 9 of 22, 0 of 126, 0 of 20, 21 of 55.

Those rows predate the mapping of `main`'s fetch states. **2026-11-02 again, with it** (2026-11-04, after the
review's fixes and the owner's decision that a challenge is an ordinary failed fetch: the read-only copy
`dq-week/lake/` plus its fetches, `dq-week/lake-fetches/`, copied into the replay's own scratch lake; a listing of
both copies' files, sizes and mtimes hashed the same before and after): tests `run_ok_rate` pass,
`adapter_ok_rate` pass, `suspect_runs_need_review` warn. **No systemic finding.** Listed 63, as before the mapping
(52 `failed`, 8 `empty`, 6 `suspect`; 3 both empty and suspect), 0 held back. The 27 WooCommerce stores that failed
with a bare 202 name no edge (these files predate raw 1.4.0's `sg-captcha` field), so they are plain `failed`
entries. 5 generic stores behind Cloudflare are counted in `by_connector.challenged`, but none is on the list,
since none has a known catalog. 0 partial and 0 quarantined: both states are newer than these files. The 11
roasters re-fetch would ask again are the same as above, so the 346 stands. The upper bound wasn't recounted: this
copy holds no snapshots.

Reproduce, from the repo root, with the lake copies cut to `<run_date>` (each root needs every table's schema
file; copy a fetches-only folder into `<out dir>/lake/fetches/` instead of passing it):

```sh
HF_HUB_OFFLINE=1 python eval/run_quality/replay_repulls.py <out dir> <run_date> <lake copy> [<lake copy> ...]
```
