"""Tests for guard.py. Run with `python3 test_guard.py` or `pytest test_guard.py`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard.py")


def run(payload, project_dir=None):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir or tempfile.mkdtemp()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run([sys.executable, GUARD], input=raw, capture_output=True,
                          text=True, env=env)
    return proc.returncode, proc.stderr


def call(tool, agent_id=None, project_dir=None, **tool_input):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    if agent_id:
        payload["agent_id"] = agent_id
    return run(payload, project_dir)[0]


def bash(command, **kw):
    return call("Bash", command=command, **kw)


ALLOW, DENY = 0, 2


def test_subagent_keeps_full_tools():
    assert call("Edit", agent_id="a1", file_path="x.py") == ALLOW
    assert call("Write", agent_id="a1", file_path="x.py", content="") == ALLOW
    assert bash("rm -rf build && make", agent_id="a1") == ALLOW


def test_subagent_cannot_flip_off_switch():
    assert bash("touch .claude/tpm.off", agent_id="a1") == DENY
    assert call("Write", agent_id="a1", file_path=".claude/tpm.off", content="") == DENY
    assert call("Read", agent_id="a1", file_path=".claude/tpm.off") == ALLOW


def test_main_thread_denies_mutating_tools():
    for tool in ("Edit", "Write", "NotebookEdit", "MultiEdit", "PowerShell", "Monitor",
                 "SomeUnknownTool", "mcp__github__create_pull_request"):
        assert call(tool, file_path="x") == DENY, tool


def test_main_thread_allows_reads_and_delegation():
    for tool in ("Read", "Grep", "Glob", "Agent", "Skill", "TodoWrite", "AskUserQuestion",
                 "mcp__github__get_file_contents", "mcp__claude-code-remote__list_events"):
        assert call(tool) == ALLOW, tool


def test_deny_message_explains_tpm_mode():
    code, err = run({"tool_name": "Edit", "tool_input": {}})
    assert code == DENY
    assert "TPM mode" in err and "Agent tool" in err and "Only the user" in err


def test_read_only_bash_allowed():
    for cmd in ("git status", "git diff --stat HEAD~1", "git log --oneline -5",
                "git show HEAD:README.md", "git branch", "git branch -a -v",
                "git branch --show-current", "git -C /tmp status", "ls -la src",
                "cat README.md", "head -20 a.py", "tail -n 5 b.log", "wc -l *.py",
                "grep -rn 'def main' src", "rg -n 'a|b' src", "find . -name '*.py'",
                "pwd", "git log | head -20", "ls src && cat README.md",
                "grep -c x f 2>/dev/null", "git diff 2>&1 | tail -5",
                "cd /home/user/x && git status"):
        assert bash(cmd) == ALLOW, cmd


def test_tricky_bash_denied():
    for cmd in ("cat x > y", "cat x >> y", "echo hi > f", "ls && rm -rf z",
                "ls; rm -rf z", "ls || rm z", "git log | sh", "find . -delete",
                "find . -name '*.pyc' -exec rm {} \\;", "find . -fprint out",
                "git commit -m x", "git push", "git checkout main", "git reset --hard",
                "git branch -D feature", "git branch new-feature", "git -c core.pager=sh log",
                "git diff --output=patch.txt", "git diff --ext-diff",
                "sed -i 's/a/b/' f.py", "rm -rf build", "python3 x.py", "touch .claude/tpm.off",
                "cat $(echo /etc/passwd)", "cat `whoami`", "ls\nrm -rf z",
                "FOO=1 ls", "/bin/ls", "rg --pre sh x", "tee out < in", "(rm x)",
                "cat <<EOF > f\nhi\nEOF", "ls 'unterminated", ""):
        assert bash(cmd) == DENY, repr(cmd)


def test_off_switch():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".claude"))
    assert call("Edit", project_dir=d, file_path="x") == DENY
    open(os.path.join(d, ".claude", "tpm.off"), "w").close()
    assert call("Edit", project_dir=d, file_path="x") == ALLOW
    assert bash("rm -rf build", project_dir=d) == ALLOW


def test_malformed_input_fails_open():
    code, err = run("not json{")
    assert code == ALLOW and "could not parse" in err
    assert run("[1, 2]")[0] == ALLOW


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
