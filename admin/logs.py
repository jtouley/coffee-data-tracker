"""The admin's structured log: one JSON object per line, with its severity, which Cloud Run reads as a structured
entry so alerts can match its fields (src/terraform/admin.tf, admin_control.tf). Never a header, cookie, token, code
or request body."""

from __future__ import annotations

import json
import logging

log = logging.getLogger("admin")


def emit(severity: str, **fields) -> None:
    level = logging.ERROR if severity == "ERROR" else logging.INFO
    log.log(level, json.dumps({"severity": severity, **fields}, default=str))


def setup() -> None:
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
