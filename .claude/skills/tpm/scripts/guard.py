#!/usr/bin/env python3
"""PreToolUse guard for the tpm skill.

Keeps the main agent in program-manager mode: it may talk, read lightly and
delegate, but core work (edits, writes, shell commands with side effects) goes
to subagents. Subagent calls (input carries `agent_id`) pass through untouched.

Exit 0 = allow, exit 2 + stderr = deny (stderr is shown to the model).
Off switch: the file $CLAUDE_PROJECT_DIR/.claude/tpm.off. Only the user creates it.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

OFF_FILE = os.path.join(".claude", "tpm.off")

DENY_MESSAGE = (
    "TPM mode is on: the main thread doesn't do core work ({what}). "
    "Delegate it with the Agent tool and a tight brief (goal, constraints, file "
    "pointers, what's ruled out, expected output shape), then review what comes back. "
    "Only the user can lift this guard; do not create .claude/tpm.off or ask an agent to."
)

# Tools the main thread may use as-is: conversation, light reads, delegation, tracking.
SAFE_TOOLS = {
    "Read", "Grep", "Glob", "LS",
    "Agent", "Task", "SendMessage", "TaskOutput", "TaskStop", "BashOutput",
    "Skill", "ToolSearch", "ListSkills", "SearchSkills", "ListPlugins", "SearchPlugins",
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
    "WebFetch", "WebSearch", "SubagentHandback",
}
# Read-only tools a subagent may use even when the input mentions the off switch.
READ_TOOLS = {"Read", "Grep", "Glob", "LS"}
# MCP tools whose verb says they only read, e.g. mcp__github__get_file_contents.
MCP_READ_VERB = re.compile(r"^(get|list|read|search|describe|view|whoami|guide)(_|$)")

SIMPLE_READERS = {"ls", "cat", "head", "tail", "wc", "grep", "pwd", "cd", "rg", "find"}
FIND_BANNED = {"-exec", "-execdir", "-ok", "-okdir", "-delete",
               "-fprint", "-fprint0", "-fprintf", "-fls"}
GIT_READ_SUBCOMMANDS = {"status", "diff", "log", "show", "branch"}
GIT_BRANCH_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
                    "-l", "--list", "--show-current", "--no-color", "--color",
                    "--contains", "--no-contains", "--merged", "--no-merged",
                    "--points-at", "--sort", "--format", "--column", "--no-column"}
GIT_BRANCH_TAKES_VALUE = {"--contains", "--no-contains", "--merged", "--no-merged",
                          "--points-at", "--sort", "--format"}
CONTROL_OPS = {"|", "||", "&&", ";", "&", ";;", "|&"}


def project_dir(payload: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def bash_is_read_only(command: str) -> tuple[bool, str]:
    """Return (ok, reason). Conservative: anything not clearly read-only is denied."""
    if "`" in command or "$(" in command or "<(" in command or ">(" in command:
        return False, "command substitution"
    try:
        lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        return False, f"unparseable command ({exc})"
    if not tokens:
        return False, "empty command"

    segments: list[list[str]] = [[]]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in CONTROL_OPS:
            segments.append([])
        elif tok in (">", ">>", "&>", "2>", "1>"):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target != "/dev/null":
                return False, "output redirection"
            if segments[-1] and segments[-1][-1].isdigit():
                segments[-1].pop()  # the fd number in `2>/dev/null`
            i += 1
        elif tok == ">&":
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if not target.isdigit():
                return False, "output redirection"
            if segments[-1] and segments[-1][-1].isdigit():
                segments[-1].pop()  # `2>&1`
            i += 1
        elif any(c in tok for c in "<>()"):
            return False, f"shell operator {tok!r}"
        else:
            segments[-1].append(tok)
        i += 1

    segments = [s for s in segments if s]
    if not segments:
        return False, "empty command"
    for seg in segments:
        ok, why = segment_is_read_only(seg)
        if not ok:
            return False, why
    return True, ""


def segment_is_read_only(seg: list[str]) -> tuple[bool, str]:
    cmd, args = seg[0], seg[1:]
    if "=" in cmd:
        return False, "environment assignment"
    if cmd == "git":
        return git_is_read_only(args)
    if cmd not in SIMPLE_READERS:
        return False, f"`{cmd}` is not on the read-only list"
    if cmd == "find" and any(a in FIND_BANNED for a in args):
        return False, "find with an action that writes or runs commands"
    if cmd == "rg" and any(a.startswith("--pre") for a in args):
        return False, "rg --pre runs a command"
    return True, ""


def git_is_read_only(args: list[str]) -> tuple[bool, str]:
    # Allow only `-C <dir>` and `--no-pager` before the subcommand; `-c` can set
    # config that runs programs, so it's refused.
    while args and args[0] in ("-C", "--no-pager"):
        args = args[2:] if args[0] == "-C" else args[1:]
    if not args:
        return False, "bare git"
    sub, rest = args[0], args[1:]
    if sub not in GIT_READ_SUBCOMMANDS:
        return False, f"`git {sub}` is not read-only"
    for a in rest:
        if a.startswith("--output") or a.startswith("--ext-diff") or a == "-o":
            return False, f"`git {sub} {a}` writes or runs programs"
    if sub == "branch":
        skip = False
        for a in rest:
            if skip:
                skip = False
                continue
            name = a.split("=", 1)[0]
            if name in GIT_BRANCH_FLAGS:
                skip = name in GIT_BRANCH_TAKES_VALUE and "=" not in a
                continue
            if a.startswith("-"):
                return False, f"`git branch {a}` changes branches"
            if "--list" not in rest and "-l" not in rest:
                return False, "`git branch <name>` creates a branch"
    return True, ""


def main_thread_decision(tool: str, tool_input: dict) -> tuple[bool, str]:
    if tool in SAFE_TOOLS:
        return True, ""
    if tool == "Bash":
        ok, why = bash_is_read_only(str(tool_input.get("command", "")))
        return ok, f"Bash: {why}"
    if tool.startswith("mcp__"):
        verb = tool.rsplit("__", 1)[-1]
        if MCP_READ_VERB.match(verb):
            return True, ""
        return False, f"{tool} may change state"
    # Edit, Write, NotebookEdit, MultiEdit, PowerShell, Monitor, worktrees and
    # anything unknown: deny.
    return False, tool


def decide(payload: dict) -> tuple[bool, str]:
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if payload.get("agent_id"):
        # Subagents keep full tools, but none of them may flip the off switch.
        if tool not in READ_TOOLS and "tpm.off" in json.dumps(tool_input):
            return False, "only the user may create or remove .claude/tpm.off"
        return True, ""
    if os.path.exists(os.path.join(project_dir(payload), OFF_FILE)):
        return True, ""
    return main_thread_decision(tool, tool_input)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except ValueError as exc:
        # Fail open so a bug or format change can't brick the session.
        print(f"tpm guard: could not parse hook input ({exc}); allowing.", file=sys.stderr)
        return 0
    allowed, what = decide(payload)
    if allowed:
        return 0
    print(DENY_MESSAGE.format(what=what), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
