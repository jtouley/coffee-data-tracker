"""Logs as one JSON object per line on stdout, the shape Cloud Logging reads (`severity`, `message`, and
`logging.googleapis.com/trace` so each request's lines group together), plus one access line per request.

Never logged: headers, bodies, query strings, cookies or tokens. The access line names the route template
(`/shot/{shot_id}`), not the path, so ids don't end up in it either.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

# Per request: the trace id and, once a route has checked the session, the user id. A dict, so a value set inside
# the route (another context copy) is still seen by the access line.
REQUEST: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("request", default=None)
access = logging.getLogger("app.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            # The stack trace in the message itself is what Error Reporting looks for.
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        out: dict[str, Any] = {
            "severity": record.levelname,
            "message": message,
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
        }
        ctx = REQUEST.get()
        if ctx and ctx.get("trace"):
            out["logging.googleapis.com/trace"] = ctx["trace"]
        out.update(getattr(record, "fields", None) or {})
        return json.dumps(out, default=str)


def setup() -> None:
    """JSON lines on stdout for the root logger. Uvicorn's own access log is off: the app writes its own, without
    the raw path."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "huggingface_hub", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def wanted() -> bool:
    """When hosted (Cloud Run reads JSON lines from stdout), or when asked for."""
    from .auth import hosted

    return hosted() or bool(os.environ.get("COFFEEDATA_JSON_LOGS"))


def trace(header: str | None) -> str | None:
    """`X-Cloud-Trace-Context: TRACE_ID/SPAN;o=1` as Cloud Logging's trace name."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    trace_id = (header or "").split("/", 1)[0]
    if not project or not trace_id.isalnum():
        return None
    return f"projects/{project}/traces/{trace_id}"


def set_user(user_id: str) -> None:
    ctx = REQUEST.get()
    if ctx is not None:
        ctx["user_id"] = user_id


def event(message: str, level: int = logging.INFO, **fields: Any) -> None:
    """A log line with structured fields (shown as JSON keys in Cloud Logging)."""
    logging.getLogger("app.events").log(level, message, extra={"fields": fields})


async def middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    ctx: dict[str, Any] = {"trace": trace(request.headers.get("x-cloud-trace-context"))}
    token = REQUEST.set(ctx)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        elapsed = time.perf_counter() - started  # one reading for both latency fields
        fields = {
            "method": request.method,
            "route": getattr(route, "path", None) or "(no route)",
            "status": status,
            "latency_ms": round(elapsed * 1000, 1),
            "user_id": ctx.get("user_id"),
            # Cloud Logging's request fields, so the console shows the status and latency (no URL: it has ids).
            "httpRequest": {
                "requestMethod": request.method,
                "status": status,
                "latency": f"{elapsed:.3f}s",
            },
        }
        level = logging.ERROR if status >= 500 else logging.INFO
        access.log(level, "%s %s %s", request.method, fields["route"], status, extra={"fields": fields})
        REQUEST.reset(token)
