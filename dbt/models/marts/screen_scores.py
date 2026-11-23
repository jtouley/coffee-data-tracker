"""Each screened place's tier, from its latest screen (docs/design/roaster-screen.md, sections 2.5 and 4.4).

One row per place: the tier coffeedata.screen_rules.tier gives its latest screen's features (int_screen_features),
the reason codes, the rule version, and next_due, the first day it may be screened again
(screen_rules.next_due, the same function the job's selection calls, computed from screened_at and never the clock).
An ops table: written beside each release for the owner's command center, never public.

PROVISIONAL: the rules are not yet scored on a sealed test (eval/roaster_screen/README.md), and nothing shows this
tier to the owner. Claude's decision on a place the rules still leave undecided (stg_screen_verdicts, screen_verdict.py) is shown beside
the tier (verdict, verdict_reason, verdict_model) and never changes it: Suggested stays the rules' alone until the
design's sealed evaluation, so every row is decided by the rules.
"""

import pyarrow as pa

from coffeedata import screen_rules, screen_verdict

SCHEMA = pa.schema(
    [
        ("roaster_id", pa.string()),
        ("screen_id", pa.string()),
        ("tier", pa.string()),
        ("decided_by", pa.string()),
        ("reasons", pa.list_(pa.string())),
        ("rule_version", pa.string()),
        ("verdict_model", pa.string()),
        ("verdict", pa.string()),
        ("verdict_reason", pa.string()),
        ("screened_at", pa.timestamp("us", tz="UTC")),  # as lake.SCHEMAS: a unit Parquet keeps
        ("next_due", pa.date32()),
    ]
)


def model(dbt, session):
    dbt.config(materialized="table")
    rows = dbt.ref("int_screen_features").to_arrow_table().to_pylist()
    verdicts = {v["screen_id"]: v for v in dbt.ref("stg_screen_verdicts").to_arrow_table().to_pylist()}
    rows.sort(key=lambda r: (r["roaster_id"], r["screened_at"], r["batch_id"] or "", r["screen_id"]))
    # transient screens in a row, ending at the place's latest (next_due's `tries`)
    tries: dict[str, int] = {}
    was: dict[str, bool] = {}
    out = []
    for r in rows:
        rid = r["roaster_id"]
        transient = screen_rules.transient(r["status"], r["refusal"])
        tries[rid] = tries.get(rid, 0) + 1 if transient and was.get(rid) else 1
        was[rid] = transient
        if not r["is_latest"]:
            continue
        tier, reasons = screen_rules.tier(r)
        # a decision is shown only while the rules still leave the place undecided: after a rule change decides it,
        # an older decision would sit beside a tier it was never about
        v = (verdicts.get(r["screen_id"]) or {}) if screen_verdict.undecided(tier, reasons) else {}
        out.append(
            {
                "roaster_id": rid,
                "screen_id": r["screen_id"],
                "tier": tier,
                "decided_by": "rules",
                "reasons": reasons,
                "rule_version": screen_rules.RULE_VERSION,
                "verdict_model": v.get("model"),
                "verdict": v.get("label"),
                "verdict_reason": v.get("reason"),
                "screened_at": r["screened_at"],
                "next_due": screen_rules.next_due(
                    r["screened_at"], r["status"], r["refusal"], tier, tries[rid]
                ),
            }
        )
    return pa.Table.from_pylist(out, schema=SCHEMA)
