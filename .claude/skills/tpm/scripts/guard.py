#!/usr/bin/env python3
"""PreToolUse guard for the tpm skill.

Keeps the main agent in program-manager mode: it may talk, read lightly and
delegate, but core work (edits, writes, shell commands with side effects) goes
to subagents. Subagent calls (input carries `agent_id`) pass through.

This stops a cooperative orchestrator from drifting into doing the work itself.
It is not a sandbox: a determined model or agent can get past a text filter.

Exit 0 = allow, exit 2 + stderr = deny (stderr is shown to the model).
Off switch: the file $CLAUDE_PROJECT_DIR/.claude/tpm.off. Only the user creates it.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys

OFF_FILE = os.path.join(".claude", "tpm.off")

DENY_MESSAGE = (
    "TPM mode is on: the main thread doesn't do core work ({what}). "
    "Delegate it with the Agent tool and a tight brief (task, inputs, constraints, "
    "target, output format, stop condition), then review what comes back. "
    "Only the user can lift this guard; do not create .claude/tpm.off or ask an agent to."
)

# Tools the main thread may use as-is: conversation, light reads, delegation, tracking.
# Keep this list in step with the "The guard" section of SKILL.md (a test checks it).
SAFE_TOOLS = {
    "Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch",
    "Agent", "Task", "Workflow", "SendMessage", "TaskOutput", "TaskStop", "BashOutput",
    "Skill", "ToolSearch", "ListSkills", "SearchSkills", "ListPlugins", "SearchPlugins",
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "SubagentHandback",
    # Child sessions, for orchestrating through Claude Code Remote.
    "mcp__claude-code-remote__create_session",
    "mcp__claude-code-remote__send_message",
    "mcp__claude-code-remote__send_later",
    "mcp__claude-code-remote__subscribe_pr_activity",
    "mcp__claude-code-remote__unsubscribe_pr_activity",
}
# MCP tools whose verb says they only read, e.g. mcp__github__get_file_contents.
MCP_READ_VERB = re.compile(r"^(get|list|read|search|describe|view|whoami|guide)(_|$)")
# Read-only tools a subagent may use even when the input mentions the off switch.
READ_TOOLS = {"Read", "Grep", "Glob", "LS"}

PLAIN_READERS = {"ls", "cat", "head", "wc", "grep", "pwd", "cd"}
FIND_BANNED = {"-exec", "-execdir", "-ok", "-okdir", "-delete",
               "-fprint", "-fprint0", "-fprintf", "-fls"}
GIT_READ_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "grep", "rev-parse"}
GIT_BRANCH_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
                    "-l", "--list", "--show-current", "--no-color", "--color",
                    "--contains", "--no-contains", "--merged", "--no-merged",
                    "--points-at", "--sort", "--format", "--column", "--no-column"}
GIT_BRANCH_TAKES_VALUE = {"--contains", "--no-contains", "--merged", "--no-merged",
                          "--points-at", "--sort", "--format"}
SED_PRINT_SCRIPT = re.compile(r"^(\d+|\$)?(,(\d+|\$))?p$")

CONTROL_OPS = {"|", "||", "&&", ";", "&", ";;", "|&"}
OUT_REDIRECTS = {">", ">>", "&>", ">|"}
# Longest first, so `>>` wins over `>`.
OPERATORS = sorted(["||", "&&", ";;", "|&", ">>", ">&", "&>", ">|", "<<<", "<<", "<&", "<>",
                    "|", "&", ";", "<", ">", "(", ")"], key=len, reverse=True)
OP_CHARS = set("|&;<>()")


class Unsafe(Exception):
    pass


def project_dir(payload: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def tokenize(command: str) -> list[tuple[str, str]]:
    """Split a shell command into ("word", text) and ("op", text) tokens.

    Only unquoted characters form operators, so `grep '|' f` is one command.
    Raises Unsafe for anything it won't reason about.
    """
    if "`" in command or "$(" in command:
        raise Unsafe("command substitution")
    tokens: list[tuple[str, str]] = []
    word: list[str] = []
    in_word = False
    i, n = 0, len(command)

    def end_word():
        nonlocal in_word
        if in_word:
            tokens.append(("word", "".join(word)))
            word.clear()
            in_word = False

    while i < n:
        c = command[i]
        if c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                raise Unsafe("unterminated quote")
            word.append(command[i + 1:j])
            in_word = True
            i = j + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise Unsafe("unterminated quote")
                if command[i] == '"':
                    break
                if command[i] == "\\" and i + 1 < n and command[i + 1] in '"\\$`\n':
                    i += 1
                word.append(command[i])
                i += 1
            in_word = True
            i += 1
        elif c == "\\":
            if i + 1 >= n:
                raise Unsafe("trailing backslash")
            if command[i + 1] != "\n":  # backslash-newline is a line continuation
                word.append(command[i + 1])
                in_word = True
            i += 2
        elif c == "\n":
            end_word()
            tokens.append(("op", ";"))
            i += 1
        elif c in " \t":
            end_word()
            i += 1
        elif c == "#" and not in_word:
            raise Unsafe("shell comment")
        elif c in OP_CHARS:
            end_word()
            op = next(o for o in OPERATORS if command.startswith(o, i))
            tokens.append(("op", op))
            i += len(op)
        else:
            if c == "$" and i + 1 < n and command[i + 1] in "'(":
                raise Unsafe("$-quoting or substitution")
            word.append(c)
            in_word = True
            i += 1
    end_word()
    return tokens


def bash_is_read_only(command: str) -> tuple[bool, str]:
    """Return (ok, reason). Conservative: anything not clearly read-only is denied."""
    try:
        tokens = tokenize(command)
        segments: list[list[str]] = [[]]
        i = 0
        while i < len(tokens):
            kind, tok = tokens[i]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ("op", "")
            if kind == "word":
                segments[-1].append(tok)
            elif tok in CONTROL_OPS:
                segments.append([])
            elif tok in OUT_REDIRECTS or tok == ">&" or tok == "<":
                if nxt[0] != "word":
                    raise Unsafe(f"redirection {tok!r} without a target")
                if tok in OUT_REDIRECTS and nxt[1] != "/dev/null":
                    raise Unsafe("output redirection")
                if tok == ">&" and not nxt[1].isdigit():
                    raise Unsafe("output redirection")
                if tok != "<" and segments[-1] and segments[-1][-1].isdigit():
                    segments[-1].pop()  # the fd number in `2>/dev/null`, `2>&1`
                i += 1  # `<` input redirection only reads; skip its file too
            else:
                raise Unsafe(f"shell operator {tok!r}")
            i += 1
        segments = [s for s in segments if s]
        if not segments:
            raise Unsafe("empty command")
        for seg in segments:
            check_segment(seg)
    except Unsafe as exc:
        return False, str(exc)
    return True, ""


def short_flags(args: list[str]) -> str:
    """All letters in short-option clusters, e.g. ['-nf', '-x'] -> 'nfx'."""
    return "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))


def check_segment(seg: list[str]) -> None:
    cmd, args = seg[0], seg[1:]
    if "=" in cmd:
        raise Unsafe("environment assignment")
    if cmd == "git":
        return check_git(args)
    if cmd in PLAIN_READERS:
        return
    if cmd == "tail":
        if any(a.startswith("--follow") for a in args) or set(short_flags(args)) & {"f", "F"}:
            raise Unsafe("tail --follow never ends")
        return
    if cmd == "find":
        if any(a in FIND_BANNED for a in args):
            raise Unsafe("find with an action that writes or runs commands")
        return
    if cmd == "rg":
        if any(a.startswith(("--pre", "--hostname-bin")) for a in args):
            raise Unsafe("rg option that runs a program")
        return
    if cmd == "sort":
        if any(a.startswith(("--output", "--compress-program")) for a in args) \
                or "o" in short_flags(args):
            raise Unsafe("sort option that writes a file or runs a program")
        return
    if cmd == "uniq":
        if len([a for a in args if not a.startswith("-")]) > 1:
            raise Unsafe("uniq with an output file")  # also catches `-f N`: deny when unsure
        return
    if cmd == "sed":
        if len(args) >= 2 and args[0] == "-n" and SED_PRINT_SCRIPT.match(args[1]) \
                and not any(a.startswith("-") for a in args[2:]):
            return
        raise Unsafe("sed is allowed only as `sed -n <address>p <files>`")
    raise Unsafe(f"`{cmd}` is not on the read-only list")


def check_git(args: list[str]) -> None:
    # Allow only `-C <dir>` and `--no-pager` before the subcommand; `-c` can set
    # config that runs programs, so it's refused.
    while args and args[0] in ("-C", "--no-pager"):
        args = args[2:] if args[0] == "-C" else args[1:]
    if not args:
        raise Unsafe("bare git")
    sub, rest = args[0], args[1:]
    if sub not in GIT_READ_SUBCOMMANDS:
        raise Unsafe(f"`git {sub}` is not read-only")
    for a in rest:
        if a.startswith(("--output", "--ext-diff", "--open-files-in-pager")) or a == "-o" \
                or (sub == "grep" and a.startswith("-O")):
            raise Unsafe(f"`git {sub} {a}` writes or runs programs")
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
                raise Unsafe(f"`git branch {a}` changes branches")
            if "--list" not in rest and "-l" not in rest:
                raise Unsafe("`git branch <name>` creates a branch")


def main_thread_decision(tool: str, tool_input: dict) -> tuple[bool, str]:
    if tool in SAFE_TOOLS:
        return True, ""
    if tool == "Bash":
        ok, why = bash_is_read_only(str(tool_input.get("command", "")))
        return ok, f"Bash: {why}"
    if tool.startswith("mcp__"):
        if MCP_READ_VERB.match(tool.rsplit("__", 1)[-1]):
            return True, ""
        return False, f"{tool} may change state"
    # Edit, Write, NotebookEdit, MultiEdit, PowerShell, Monitor, worktrees and
    # anything unknown: deny.
    return False, tool


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_in(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings_in(v)]
    return []


def mentions_off_switch(tool_input: dict) -> bool:
    # Strip shell quoting so `tpm.of""f` and `tpm.of\f` still match. A filter like
    # this can still be dodged (globs, variables); see the threat model above.
    text = "".join(strings_in(tool_input))
    return "tpm.off" in re.sub(r"[\"'\\\s]", "", text)


def decide(payload: dict) -> tuple[bool, str]:
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if payload.get("agent_id"):
        # Subagents keep full tools, but none of them may flip the off switch.
        if tool not in READ_TOOLS and mentions_off_switch(tool_input):
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
