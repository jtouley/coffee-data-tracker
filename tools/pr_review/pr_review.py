"""The OpenCode PR review: diff preparation, running the reviewer, and reading its answer. Standard library only, so
CI needs no `uv sync`. The reviewer itself is `.opencode/agents/pr-reviewer.md`.

    git diff A...B | python pr_review.py prep [--max-chars N]          annotated diff on stdout
    python pr_review.py review <diff> --models M1,M2 --pr N [--out F]   first model that answers; JSON result
    python pr_review.py comment <result.json>                           the PR comment's markdown on stdout
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

MAX_CHARS = 120_000  # about 30k tokens: fits every free model's context with room for the answer
MAX_FILE_LINES = 600  # per file, so one generated file can't crowd out the rest
COMMENT_MARKER = "<!-- opencode-pr-review -->"
SKIP = (
    "*.lock",
    "uv.lock",
    "package-lock.json",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.pdf",
    "*.parquet",
    "*.duckdb",
    "*.ipynb",
    "tests/fixtures/*",
    "eval/sealed/*",
    "*/SHA256SUMS",
    "licenses/*",
)
SEVERITIES = ("blocker", "major", "minor", "nit")


# ---- diff preparation ----


@dataclass
class DiffLine:
    kind: str  # "+", "-", " "
    text: str
    new_no: int | None  # line number in the new file; None for removed lines


@dataclass
class Hunk:
    header: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    status: str  # added, deleted, modified, renamed
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


def parse_diff(text: str) -> list[FileDiff]:
    """A `git diff` (unified, any context) as files, hunks and lines with new-file line numbers."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    new_no = 0
    old_path = ""
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            path = m.group(2) if m else raw.split()[-1]
            cur = FileDiff(path=path, status="modified")
            files.append(cur)
            hunk = None
            old_path = m.group(1) if m else path
            continue
        if cur is None:
            continue
        if hunk is None:
            if raw.startswith("new file mode"):
                cur.status = "added"
            elif raw.startswith("deleted file mode"):
                cur.status = "deleted"
            elif raw.startswith("rename to ") or (
                raw.startswith("similarity index") and old_path != cur.path
            ):
                cur.status = "renamed"
            elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                cur.binary = True
            elif raw.startswith("+++ "):
                target = raw[4:]
                if target != "/dev/null":
                    cur.path = target[2:] if target.startswith("b/") else target
        m = _HUNK.match(raw)
        if m:
            hunk = Hunk(header=raw)
            cur.hunks.append(hunk)
            new_no = int(m.group(1))
            continue
        if hunk is None:
            continue
        if raw.startswith("+"):
            hunk.lines.append(DiffLine("+", raw[1:], new_no))
            new_no += 1
        elif raw.startswith("-"):
            hunk.lines.append(DiffLine("-", raw[1:], None))
        elif raw.startswith(" ") or raw == "":
            hunk.lines.append(DiffLine(" ", raw[1:], new_no))
            new_no += 1
        # "\ No newline at end of file" and anything else: skip
    return files


def skipped(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, pat) or fnmatch(name, pat) for pat in SKIP)


def render_file(f: FileDiff, max_lines: int = MAX_FILE_LINES) -> str:
    out = [f"### {f.path} ({f.status})"]
    shown = 0
    width = max((ln.new_no or 0 for h in f.hunks for ln in h.lines), default=0)
    width = max(len(str(width)), 4)
    for h in f.hunks:
        if shown >= max_lines:
            out.append(f"[... {sum(len(x.lines) for x in f.hunks) - shown} more lines of this file left out]")
            break
        out.append(h.header)
        for ln in h.lines:
            if shown >= max_lines:
                break
            no = str(ln.new_no) if ln.new_no is not None else ""
            out.append(f"{no:>{width}} {ln.kind} {ln.text}")
            shown += 1
    return "\n".join(out)


def prepare(diff_text: str, max_chars: int = MAX_CHARS) -> tuple[str, dict]:
    """The annotated diff the reviewer reads, and what was left out."""
    return prepare_files(parse_diff(diff_text), max_chars)


def shown_lines(f: FileDiff, max_lines: int = MAX_FILE_LINES) -> list[DiffLine]:
    """The lines of `f` that `render_file` shows."""
    return [ln for h in f.hunks for ln in h.lines][:max_lines]


def prepare_files(files: list[FileDiff], max_chars: int = MAX_CHARS) -> tuple[str, dict]:
    kept, left_out, parts = [], [], []
    total = 0
    for f in files:
        if f.binary or skipped(f.path) or not f.hunks:
            left_out.append(f.path)
            continue
        block = render_file(f)
        if total + len(block) > max_chars:
            left_out.append(f.path)
            continue
        parts.append(block)
        kept.append(f.path)
        total += len(block) + 2
    if left_out:
        parts.append("[Left out of this diff: " + ", ".join(left_out) + "]")
    return "\n\n".join(parts) + "\n", {"files": kept, "left_out": left_out, "chars": total}


# ---- the reviewer's answer ----


_FENCE = re.compile(r"```json\s*\n(.*?)\n\s*```", re.S)


def parse_answer(text: str) -> dict:
    """The review markdown and the last JSON block, checked. `ok` is False when the JSON is missing or malformed."""
    blocks = _FENCE.findall(text)
    markdown = _FENCE.sub("", text).strip()
    result: dict = {"ok": False, "markdown": markdown, "decision": None, "findings": [], "error": None}
    if not blocks:
        result["error"] = "no json block"
        return result
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        result["error"] = f"bad json: {e}"
        return result
    if not isinstance(data, dict):
        result["error"] = "json is not an object"
        return result
    decision = str(data.get("decision", "")).strip().upper().replace("_", " ")
    if decision not in ("MERGE", "NO MERGE"):
        result["error"] = f"bad decision: {data.get('decision')!r}"
        return result
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        try:
            line = int(f.get("line")) if f.get("line") is not None else None
        except (TypeError, ValueError):
            line = None
        sev = str(f.get("severity", "")).lower()
        findings.append(
            {
                "file": str(f.get("file", "")).strip().lstrip("./"),
                "line": line,
                "severity": sev if sev in SEVERITIES else "minor",
                "title": str(f.get("title", "")),
                "detail": str(f.get("detail", "")),
            }
        )
    result.update(ok=True, decision=decision, findings=findings)
    return result


def text_from_events(stdout: str) -> str:
    """The assistant's text from `opencode run --format json` output (one JSON event per line)."""
    texts = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") if isinstance(ev, dict) else None
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
    return "\n".join(texts) if texts else stdout


def usage_from_events(stdout: str) -> dict:
    tokens: dict = {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") if isinstance(ev, dict) else None
        if isinstance(part, dict) and isinstance(part.get("tokens"), dict):
            for k, v in part["tokens"].items():
                if isinstance(v, int | float):
                    tokens[k] = tokens.get(k, 0) + v
    return tokens


# ---- the OpenCode API key ----

# In GitHub Actions the key is minted from the job's OIDC token (permissions: id-token: write), so no long-lived
# secret is stored. OPENCODE_CLIENT_ID names the OpenCode credential that trusts this repo. Outside Actions, or
# when OPENCODE_API_KEY is set, that key is used as is.
REFRESH_EARLY = 300  # seconds: mint a new key this long before the old one expires
_minted: dict = {}


def exchange_oidc(client_id: str) -> tuple[str, float]:
    """An OpenCode API key for this Actions job, and when it expires (epoch seconds). The exchange is
    opencode-login.sh, plain curl, so a failure says which side refused and shows OpenCode's error."""
    proc = subprocess.run(
        ["bash", str(Path(__file__).with_name("opencode-login.sh"))],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "OPENCODE_CLIENT_ID": client_id},
    )
    if proc.returncode != 0:
        raise OSError(" ".join((proc.stdout + " " + proc.stderr).split())[-500:])
    tok = json.loads(proc.stdout)
    key = tok["access_token"]
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{key}", flush=True)
    return key, time.time() + float(tok.get("expires_in") or 1800)


def api_key() -> str | None:
    if os.environ.get("OPENCODE_API_KEY"):
        return os.environ["OPENCODE_API_KEY"]
    client_id = os.environ.get("OPENCODE_CLIENT_ID")
    if not client_id or "ACTIONS_ID_TOKEN_REQUEST_URL" not in os.environ:
        return None
    if not _minted or _minted["expires"] - time.time() < REFRESH_EARLY:
        _minted["key"], _minted["expires"] = exchange_oidc(client_id)
    return _minted["key"]


# ---- running the reviewer ----


def log_errors(stderr: str | bytes | None, keep: int = 3) -> str:
    """The last few error lines from OpenCode's stderr (--print-logs), or its last lines if none say error."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if "level=ERROR" in ln or "error" in ln.lower()]
    return " | ".join((errors or lines)[-keep:])[-900:]


def run_model(
    model: str, message: str, workdir: Path, timeout: int = 600, opencode: str = "opencode"
) -> tuple[str | None, dict]:
    """One `opencode run` with the pr-reviewer agent. Returns (answer text or None, run info)."""
    env = dict(os.environ)
    # Belt and braces: no tool may run whatever the checked-out config says.
    env["OPENCODE_PERMISSION"] = json.dumps({"*": "deny"})
    try:
        key = api_key()
    except (OSError, KeyError, ValueError) as e:  # a failed exchange is an unanswered run, not a crash
        return None, {"model": model, "error": f"OpenCode sign-in failed: {e}", "seconds": 0}
    if key:
        env["OPENCODE_API_KEY"] = key
    # A fixed --title skips OpenCode's session-title call, which goes to a separate small model. --print-logs puts
    # OpenCode's warnings and errors on stderr, so a failed or hung run says why.
    cmd = [
        opencode,
        "run",
        "--pure",
        "--agent",
        "pr-reviewer",
        "-m",
        model,
        "--format",
        "json",
        "--title",
        "pr-review",
    ]
    cmd += ["--print-logs", "--log-level", "WARN"]
    cmd.append("Review the pull request below. Follow your output instructions exactly.")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, input=message, capture_output=True, text=True, cwd=workdir, env=env, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        why = log_errors(e.stderr)
        return None, {
            "model": model,
            "error": f"timeout after {timeout}s" + (f"; {why}" if why else ""),
            "seconds": timeout,
        }
    seconds = round(time.monotonic() - started, 1)
    text = text_from_events(proc.stdout).strip()
    info = {
        "model": model,
        "seconds": seconds,
        "exit": proc.returncode,
        "tokens": usage_from_events(proc.stdout),
    }
    if proc.returncode != 0 or not text:
        info["error"] = log_errors(proc.stderr) or "no output"
        return None, info
    return text, info


def review(
    diff: str, models: list[str], title: str, workdir: Path, timeout: int = 600, tries: int = 2
) -> dict:
    """Try each model in turn (free models come and go, and rate-limit) until one gives a parseable answer."""
    message = f"{title}\n\n{diff}"
    attempts = []
    for model in models:
        for attempt in range(tries):
            text, info = run_model(model, message, workdir, timeout)
            attempts.append(info)
            if text is not None:
                parsed = parse_answer(text)
                if parsed["ok"]:
                    return {**parsed, "model": model, "raw": text, "attempts": attempts}
                info["error"] = parsed["error"]
            if attempt + 1 < tries:
                time.sleep(10 * (attempt + 1))
    return {"ok": False, "model": None, "attempts": attempts}


# ---- the PR comment ----


def comment(result: dict, max_chars: int = 60_000) -> str:
    if not result.get("ok"):
        tried = ", ".join(sorted({a["model"] for a in result.get("attempts", [])}))
        last = (result.get("attempts") or [{}])[-1].get("error", "")
        body = f"The OpenCode review didn't run: no model answered ({tried}). Last error: `{last}`"
    else:
        body = result["markdown"]
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n[cut off]"
    model = result.get("model") or "none"
    return f"{COMMENT_MARKER}\n{body}\n\n<sub>OpenCode review · `{model}` · advisory, doesn't block merge</sub>\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--max-chars", type=int, default=MAX_CHARS)
    r = sub.add_parser("review")
    r.add_argument("diff", type=Path, help="annotated diff from `prep`")
    r.add_argument("--models", required=True, help="comma-separated provider/model, tried in order")
    r.add_argument("--pr", required=True)
    r.add_argument("--workdir", type=Path, default=Path("."))
    r.add_argument("--timeout", type=int, default=600)
    r.add_argument("--out", type=Path)
    c = sub.add_parser("comment")
    c.add_argument("result", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "prep":
        text, _ = prepare(sys.stdin.read(), a.max_chars)
        sys.stdout.write(text)
    elif a.cmd == "review":
        models = [m.strip() for m in a.models.split(",") if m.strip()]
        res = review(a.diff.read_text(), models, f"Pull request #{a.pr}.", a.workdir, a.timeout)
        out = json.dumps(res, indent=2)
        if a.out:
            a.out.write_text(out)
        else:
            print(out)
        return 0 if res["ok"] else 1
    elif a.cmd == "comment":
        sys.stdout.write(comment(json.loads(a.result.read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
