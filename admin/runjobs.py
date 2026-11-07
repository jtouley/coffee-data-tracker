"""Cloud Run Jobs, as the admin uses them: start a pipeline job with argument overrides, list and read its
executions, cancel one (docs/design/admin-registry.md, "Jobs"). The Cloud Run Admin API v2 over HTTPS, with the
service account's own token from the metadata server. Its IAM (src/terraform/admin.tf) allows this on the named
pipeline jobs only: no other job, no job updates, no Terraform.

`Execution` is the one shape the pages read; `FakeJobs` (tests) has the same methods.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from .store import MetadataToken

API = "https://run.googleapis.com/v2"
JOB = re.compile(r"[a-z][a-z0-9-]{0,62}")
EXECUTION = re.compile(
    r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z][a-z0-9-]{0,62}/executions/[a-z0-9-]+"
)
LOGS = re.compile(r"https://console\.cloud\.google\.com/\S+")


class JobsError(RuntimeError):
    """The Cloud Run API refused or didn't answer. Nothing was started (or cancelled) as far as the admin knows."""


@dataclass
class Execution:
    name: str  # projects/<p>/locations/<r>/jobs/<job>/executions/<id>
    job: str
    status: str  # pending, running, succeeded, failed, cancelled
    created: datetime | None = None
    started: datetime | None = None
    completed: datetime | None = None
    log_uri: str | None = None
    message: str | None = None  # the Completed condition's message on a failure

    @property
    def short(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    @property
    def running(self) -> bool:
        return self.status in ("pending", "running")

    @property
    def duration_s(self) -> float | None:
        if self.started and (end := self.completed):
            return (end - self.started).total_seconds()
        return None


def logs_url(name: str) -> str | None:
    """The Cloud console's logs page of an execution, from its name alone (what Cloud Run's logUri points to), so a
    finished run's link needs no API call."""
    if not EXECUTION.fullmatch(name):
        return None
    _, project, _, region, _, _job, _, short = name.split("/")
    return f"https://console.cloud.google.com/run/jobs/executions/details/{region}/{short}/logs?project={project}"


def _t(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse(e: dict) -> Execution:
    """An Execution resource (v2) as the pages show it."""
    name = str(e.get("name") or "")
    task_count = int(e.get("taskCount") or 1)
    if e.get("completionTime"):
        if int(e.get("cancelledCount") or 0):
            status = "cancelled"
        elif int(e.get("succeededCount") or 0) >= task_count:
            status = "succeeded"
        else:
            status = "failed"
    else:
        status = "running" if e.get("startTime") else "pending"
    completed: dict = next((c for c in e.get("conditions") or [] if c.get("type") == "Completed"), {})
    log = e.get("logUri")
    return Execution(
        name=name,
        job=(e.get("job") or name.split("/jobs/")[-1].split("/")[0]).rsplit("/", 1)[-1],
        status=status,
        created=_t(e.get("createTime")),
        started=_t(e.get("startTime")),
        completed=_t(e.get("completionTime")),
        log_uri=log if isinstance(log, str) and LOGS.fullmatch(log) else None,
        message=(completed.get("message") or None) if status == "failed" else None,
    )


class Jobs(Protocol):
    def run(self, job: str, args: list[str] | None) -> Execution: ...
    def executions(self, job: str, limit: int = 10) -> list[Execution]: ...
    def execution(self, name: str) -> Execution: ...
    def cancel(self, name: str) -> None: ...


class CloudRunJobs:
    def __init__(self, project: str, region: str, client: httpx.Client | None = None, timeout: float = 15.0):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project) or not re.fullmatch(
            r"[a-z]+-[a-z]+\d", region
        ):
            raise ValueError("bad project or region")
        self.parent = f"projects/{project}/locations/{region}"
        self.http = client or httpx.Client(timeout=timeout)
        self.token = MetadataToken(self.http, JobsError)

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.http.request(method, f"{API}/{path}", headers=self.token.header(), **kw)
        except httpx.HTTPError as e:
            raise JobsError(f"Cloud Run didn't answer: {type(e).__name__}") from e
        if r.status_code >= 400:
            try:
                msg = str((r.json().get("error") or {}).get("message") or "")[:200]
            except ValueError:
                msg = ""
            raise JobsError(f"Cloud Run refused ({r.status_code}){': ' + msg if msg else ''}")
        try:
            return r.json()
        except ValueError as e:
            raise JobsError("Cloud Run's answer wasn't JSON") from e

    def run(self, job: str, args: list[str] | None) -> Execution:
        if not JOB.fullmatch(job):
            raise ValueError("bad job name")
        body = {} if args is None else {"overrides": {"containerOverrides": [{"args": args}]}}
        op = self._call("POST", f"{self.parent}/jobs/{job}:run", json=body)
        meta = op.get("metadata") or {}
        if not EXECUTION.fullmatch(str(meta.get("name") or "")):
            raise JobsError("Cloud Run started the job but didn't name the execution")
        return parse(meta)

    def executions(self, job: str, limit: int = 10) -> list[Execution]:
        if not JOB.fullmatch(job):
            raise ValueError("bad job name")
        body = self._call("GET", f"{self.parent}/jobs/{job}/executions", params={"pageSize": limit})
        return [parse(e) for e in body.get("executions") or []]

    def execution(self, name: str) -> Execution:
        if not EXECUTION.fullmatch(name) or not name.startswith(self.parent + "/"):
            raise ValueError("bad execution name")
        return parse(self._call("GET", name))

    def cancel(self, name: str) -> None:
        if not EXECUTION.fullmatch(name) or not name.startswith(self.parent + "/"):
            raise ValueError("bad execution name")
        self._call("POST", f"{name}:cancel", json={})
