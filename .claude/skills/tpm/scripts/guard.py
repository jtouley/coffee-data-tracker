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
    "Delegate it with the Agent tool: a one- or two-line prompt (skill or verb, target, where the "
    "result goes) that points to the workstream doc on the orchestration branch. Workstream docs "
    "are written by a scribe agent, not by you. "
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

# Per-command option allowlists. Long options must match exactly (GNU tools and git
# accept abbreviations like `--out` for `--output`, so a denylist can't work).
# `flags`: short options without a value; `values`: short options that take one;
# "*" in flags allows any short letter (for commands with no dangerous short options);
# `numeric`: allow `-20` style counts; `max_pos`: most positional args allowed.
SPECS = {
    "ls": dict(flags="*", long={
        "--all", "--almost-all", "--author", "--block-size", "--classify", "--color",
        "--dereference", "--directory", "--file-type", "--format", "--full-time",
        "--group-directories-first", "--hide", "--human-readable", "--ignore", "--inode",
        "--indicator-style", "--literal", "--no-group", "--numeric-uid-gid", "--quoting-style",
        "--recursive", "--reverse", "--size", "--sort", "--time", "--time-style", "--width"}),
    "cat": dict(flags="AbeEnstTuv", long={
        "--show-all", "--number-nonblank", "--show-ends", "--number", "--squeeze-blank",
        "--show-tabs", "--show-nonprinting"}),
    "head": dict(flags="qvz", values="nc", numeric=True, long={
        "--lines", "--bytes", "--quiet", "--silent", "--verbose", "--zero-terminated"}),
    # No -f/-F/--follow/-s: following never ends.
    "tail": dict(flags="qvz", values="nc", numeric=True, long={
        "--lines", "--bytes", "--quiet", "--silent", "--verbose", "--zero-terminated"}),
    "wc": dict(flags="cmlLw", long={
        "--bytes", "--chars", "--lines", "--max-line-length", "--words"}),
    "pwd": dict(flags="LP", long={"--logical", "--physical"}),
    "cd": dict(flags="LP", long=set()),
    "grep": dict(flags="EFGPiyvwxcoqsbHhnTZzlLrRaIU", values="ABCmefdD", numeric=True, long={
        "--extended-regexp", "--fixed-strings", "--basic-regexp", "--perl-regexp", "--regexp",
        "--file", "--ignore-case", "--no-ignore-case", "--word-regexp", "--line-regexp",
        "--invert-match", "--count", "--color", "--colour", "--files-with-matches",
        "--files-without-match", "--max-count", "--only-matching", "--quiet", "--silent",
        "--no-messages", "--byte-offset", "--with-filename", "--no-filename", "--label",
        "--line-number", "--initial-tab", "--null", "--null-data", "--after-context",
        "--before-context", "--context", "--text", "--binary-files", "--devices",
        "--directories", "--exclude", "--exclude-dir", "--include", "--recursive",
        "--dereference-recursive", "--line-buffered", "--binary"}),
    # No --pre, --hostname-bin or other options that run programs.
    "rg": dict(flags="iSswxvclnNoqFPUuLHhpaz0.", values="ABCmMegtTfrjd", long={
        "--ignore-case", "--smart-case", "--case-sensitive", "--word-regexp", "--line-regexp",
        "--invert-match", "--count", "--count-matches", "--files-with-matches",
        "--files-without-match", "--files", "--line-number", "--no-line-number",
        "--only-matching", "--quiet", "--replace", "--regexp", "--file", "--glob", "--iglob",
        "--type", "--type-not", "--type-list", "--after-context", "--before-context",
        "--context", "--max-count", "--max-depth", "--max-columns", "--hidden", "--no-ignore",
        "--no-ignore-vcs", "--follow", "--heading", "--no-heading", "--with-filename",
        "--no-filename", "--color", "--colors", "--column", "--json", "--stats", "--sort",
        "--sortr", "--fixed-strings", "--multiline", "--multiline-dotall", "--pcre2",
        "--text", "--null", "--vimgrep", "--trim", "--unrestricted", "--search-zip"}),
    # No -o/--output (writes a file) or --compress-program (runs one).
    "sort": dict(flags="bdfghiMnRrsuVzcC", values="ktS", long={
        "--ignore-leading-blanks", "--dictionary-order", "--ignore-case",
        "--general-numeric-sort", "--human-numeric-sort", "--ignore-nonprinting",
        "--month-sort", "--numeric-sort", "--random-sort", "--reverse", "--version-sort",
        "--check", "--key", "--field-separator", "--stable", "--unique", "--zero-terminated",
        "--buffer-size", "--parallel"}),
    # A second positional is an output file; `-` counts as a positional.
    "uniq": dict(flags="cdDuiz", values="fsw", max_pos=1, long={
        "--count", "--repeated", "--all-repeated", "--unique", "--ignore-case",
        "--zero-terminated", "--skip-fields", "--skip-chars", "--check-chars", "--group"}),
}
# find's options are single-dash predicates; only these are allowed.
FIND_PREDICATES = {
    "-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
    "-type", "-xtype", "-maxdepth", "-mindepth", "-mtime", "-mmin", "-atime", "-amin",
    "-ctime", "-cmin", "-newer", "-size", "-empty", "-perm", "-user", "-group", "-links",
    "-readable", "-writable", "-executable", "-print", "-print0", "-printf", "-prune",
    "-not", "-o", "-or", "-a", "-and", "-true", "-false", "-depth", "-xdev", "-mount",
    "-follow", "-L", "-H", "-P"}
GIT_READ_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "grep", "rev-parse"}
GIT_DIFF_LOG_LONG = {
    "--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "--summary",
    "--patch", "--no-patch", "--unified", "--cached", "--staged", "--color", "--no-color",
    "--color-words", "--word-diff", "--ignore-all-space", "--ignore-space-change",
    "--ignore-blank-lines", "--minimal", "--patience", "--histogram", "--diff-filter",
    "--find-renames", "--find-copies", "--no-renames", "--relative", "--abbrev",
    "--full-index", "--raw", "--dirstat", "--compact-summary", "--check", "--exit-code",
    "--quiet", "--merge-base", "--oneline", "--graph", "--decorate", "--no-decorate",
    "--all", "--branches", "--tags", "--remotes", "--author", "--committer", "--grep",
    "--since", "--until", "--after", "--before", "--max-count", "--skip", "--reverse",
    "--first-parent", "--merges", "--no-merges", "--format", "--pretty", "--date",
    "--follow", "--source", "--left-right", "--cherry-pick", "--ancestry-path",
    "--topo-order", "--date-order", "--no-walk", "--boundary", "--simplify-by-decoration"}
GIT_LONG = {
    "status": {"--short", "--branch", "--porcelain", "--long", "--untracked-files",
               "--ignored", "--ignore-submodules", "--show-stash", "--ahead-behind",
               "--no-ahead-behind", "--renames", "--no-renames", "--column", "--no-column",
               "--verbose"},
    "diff": GIT_DIFF_LOG_LONG, "log": GIT_DIFF_LOG_LONG, "show": GIT_DIFF_LOG_LONG,
    "rev-parse": {"--abbrev-ref", "--short", "--verify", "--show-toplevel", "--git-dir",
                  "--is-inside-work-tree", "--show-prefix", "--symbolic-full-name",
                  "--quiet", "--absolute-git-dir", "--git-common-dir", "--show-cdup"},
    "grep": {"--line-number", "--count", "--ignore-case", "--word-regexp", "--invert-match",
             "--files-with-matches", "--files-without-match", "--name-only",
             "--extended-regexp", "--fixed-strings", "--perl-regexp", "--basic-regexp",
             "--cached", "--untracked", "--heading", "--break", "--show-function",
             "--function-context", "--context", "--after-context", "--before-context",
             "--max-depth", "--all-match", "--and", "--or", "--not", "--color", "--no-color",
             "--full-name", "--null", "--column", "--only-matching", "--quiet",
             "--exclude-standard"},
}
# Short letters refused per git subcommand (git grep -O opens files in a program).
GIT_SHORT_BANNED = {"grep": "O"}
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


def check_options(cmd: str, args: list[str], spec: dict) -> list[str]:
    """Validate options against an allowlist; return the positional args."""
    flags, values = spec.get("flags", ""), spec.get("values", "")
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            positionals.extend(args[i:])
            break
        if a.startswith("--"):
            if a.split("=", 1)[0] not in spec["long"]:
                raise Unsafe(f"`{cmd} {a}` is not an allowed option")
        elif a.startswith("-") and a != "-":
            body = a[1:]
            if spec.get("numeric") and body.isdigit():
                continue
            for j, c in enumerate(body):
                if c in values:
                    if j == len(body) - 1:
                        i += 1  # value is the next argument
                    break  # the rest of the cluster is the value
                if flags != "*" and c not in flags:
                    raise Unsafe(f"`{cmd} -{c}` is not an allowed option")
                if flags == "*" and not c.isalnum():
                    raise Unsafe(f"`{cmd} {a}` is not an allowed option")
        else:
            positionals.append(a)
    if len(positionals) > spec.get("max_pos", 10**9):
        raise Unsafe(f"`{cmd}` with more than {spec['max_pos']} file argument(s)")
    return positionals


def check_segment(seg: list[str]) -> None:
    cmd, args = seg[0], seg[1:]
    if "=" in cmd:
        raise Unsafe("environment assignment")
    if cmd == "git":
        return check_git(args)
    if cmd in SPECS:
        check_options(cmd, args, SPECS[cmd])
        return
    if cmd == "find":
        for a in args:
            if a.startswith("-") and a not in FIND_PREDICATES:
                raise Unsafe(f"`find {a}` is not an allowed predicate")
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
    if sub == "branch":
        return check_git_branch(rest)
    banned = GIT_SHORT_BANNED.get(sub, "")
    for a in rest:
        if a == "--":
            break
        if a.startswith("--"):
            if a.split("=", 1)[0] not in GIT_LONG[sub]:
                raise Unsafe(f"`git {sub} {a}` is not an allowed option")
        elif a.startswith("-") and any(c in banned for c in a[1:]):
            raise Unsafe(f"`git {sub} {a}` runs a program")


def check_git_branch(rest: list[str]) -> None:
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
            raise Unsafe(f"`git branch {a}` is not a listing option")
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
