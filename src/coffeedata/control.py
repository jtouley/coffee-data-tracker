"""The control plane: the job_manifests table (the manifest/control contract).

Every job writes one row when it starts (status running) and one when it ends (done, deadline or failed): which
job, connector, shard and week, its counts, the egress IP(s) it saw and each IP's first-contact refusals, and the
code version. Services read it instead of calling each other: the merge sees which connectors landed, the
enrichment job sees that the merge did, and the egress check flags an IP that keeps getting refused. Scheduling is
Cloud Scheduler's (time-based); this table says what actually ran.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pyarrow.parquet as pq

from . import lineage
from .lake import SCHEMAS, Lake, new_batch_id

REFUSAL_FLAG = (
    0.08  # an IP refused on more than 8% of first contacts, two runs in a row, is flagged (DECISIONS)
)


def now() -> datetime:
    from . import ingest  # its clock, which the simulation scales

    return ingest.datetime.now(UTC).replace(microsecond=0)


def egress_ip() -> str | None:
    """The IP this job's requests leave from (http.egress_ip: COFFEEDATA_EGRESS_IP, else an echo lookup)."""
    from . import http

    return http.egress_ip()


def _path(lake: Lake, run_date: str, name: str):
    return lake.root / "job_manifests" / f"run_date={run_date}" / f"attempt={name}.parquet"


def write(lake: Lake, row: dict) -> None:
    schema = SCHEMAS["job_manifests"]
    full = {f.name: row.get(f.name) for f in schema}
    full["run_date"] = date.fromisoformat(str(full["run_date"]))
    if isinstance(full.get("detail"), dict):
        full["detail"] = json.dumps(full["detail"], default=str)
    path = _path(lake, str(full["run_date"]), f"{full['attempt_id']}__{full['status']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    import pyarrow as pa

    tmp = path.with_suffix(".tmp")
    pq.write_table(pa.Table.from_pylist([full], schema=schema), tmp, compression="zstd")
    tmp.replace(path)


def start(lake: Lake, job: str, run_date: str, connector: str | None = None, todo: int | None = None) -> dict:
    row = {"run_date": run_date, "job": job, "connector": connector, "shard": None, "attempt_id": new_batch_id(),
           "started_at": now(), "status": "running", "todo": todo, "code_version": lineage.code_version(),
           "egress_ips": [ip] if (ip := egress_ip()) else []}  # fmt: skip
    write(lake, row)
    return row


def finish(lake: Lake, row: dict, status: str, **counts) -> dict:
    row = row | counts | {"ended_at": now(), "status": status}
    if (fc := counts.get("first_contact")) is not None:
        by_ip = fc.get("by_ip") or {
            egress_ip() or "unknown": {"stores": fc["stores"], "refused": fc["refused"]}
        }
        row["refusals"] = [{"ip": ip, "stores": c.get("stores", 0), "refused": c.get("refused", 0)}
                           for ip, c in sorted(by_ip.items())]  # fmt: skip
    # every egress IP the job saw: at its start, at its end, and on its first contacts
    seen = [*(row.get("egress_ips") or []), egress_ip(), *(x["ip"] for x in row.get("refusals") or [])]
    row["egress_ips"] = sorted({ip for ip in seen if ip and ip != "unknown"})
    row.pop("first_contact", None)
    write(lake, row)
    return row


def rows(lake: Lake, run_date: str | None = None) -> list[dict]:
    """The latest row per attempt (an attempt with only a 'running' row died: killed or timed out)."""
    where = "where run_date = ?" if run_date else ""
    q = f"""select * from {lake.scan("job_manifests")} {where}
            qualify row_number() over (partition by attempt_id order by (status <> 'running') desc,
                                       ended_at desc nulls last) = 1 order by started_at"""
    cur = lake.connect().execute(q, [date.fromisoformat(run_date)] if run_date else [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall() if r[cols.index("attempt_id")]]


def is_arm(row: dict) -> bool:
    """A confirmation-run arm's crawl (`crawl --slice`, or COFFEEDATA_ARM set): its detail records the slice or the
    arm. It writes to a slice sub-partition the merge never reads, so it never counts as the week's crawl, and its
    refusals never count towards the egress flag of production runs."""
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = None
    return isinstance(detail, dict) and bool(detail.get("slice") or detail.get("arm"))


def landed(lake: Lake, run_date: str, job: str = "crawl") -> set[str]:
    """Connectors with a finished `job` this week (done or deadline: their rows are in their partition). A
    confirmation-run arm (is_arm) never lands: its rows are in a slice sub-partition, not the connector's."""
    return {
        r["connector"]
        for r in rows(lake, run_date)
        if r["job"] == job and r["status"] in ("done", "deadline") and not is_arm(r)
    }


def rollup(lake: Lake, run_date: str) -> int:
    """Fold a week's row files into one (the weekly merge does it): no small files in the control table."""
    import pyarrow as pa

    d = lake.root / "job_manifests" / f"run_date={run_date}"
    files = sorted(d.glob("attempt=*.parquet"))
    if len(files) <= 1:
        return len(files)
    tbl = pa.concat_tables([pq.read_table(f).cast(SCHEMAS["job_manifests"]) for f in files])
    out = d / f"attempt=rollup-{new_batch_id()}.parquet"
    pq.write_table(tbl, out.with_suffix(".tmp"), compression="zstd")
    out.with_suffix(".tmp").replace(out)
    for f in files:
        f.unlink()
    return tbl.num_rows


def egress_flags(lake: Lake, job: str = "crawl") -> list[dict]:
    """IPs whose first-contact refusal share was above REFUSAL_FLAG on each of their last two runs of `job` (the
    egress-check job exits non-zero; no automatic action). Confirmation-run arms (is_arm) are left out: they are
    experiments, not production runs."""
    by_ip: dict[str, list[tuple]] = {}
    for r in rows(lake):
        if r["job"] != job or r["status"] == "running" or is_arm(r):
            continue
        for x in r["refusals"] or []:
            by_ip.setdefault(x["ip"], []).append((r["started_at"], x["stores"], x["refused"]))
    out = []
    for ip, runs in by_ip.items():
        last = sorted(runs)[-2:]
        if len(last) == 2 and all(n and k / n > REFUSAL_FLAG for _, n, k in last):
            out.append({"ip": ip, "shares": [round(k / n, 3) for _, n, k in last]})
    return out
