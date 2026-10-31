"""Tests for guard.py. Run with `python3 test_guard.py` or `pytest test_guard.py`."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")
SKILL_DIR = os.path.dirname(HERE)
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")


def load_guard():
    spec = importlib.util.spec_from_file_location("tpm_guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frontmatter():
    return open(SKILL_MD).read().split("\n---\n", 1)[0]


def hook_command():
    m = re.search(r"^\s*command: '(.*)'$", frontmatter(), re.M)
    return m.group(1).replace("''", "'")


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


# Regression tests for the independent review's findings.

def test_hash_is_not_a_comment_escape():
    for cmd in ("ls x#; touch /tmp/pwned", "cat a#b\nrm -rf z", "ls # note", "ls x#\nrm z"):
        assert bash(cmd) == DENY, repr(cmd)
    for cmd in ("cat a#b", "grep -n '#include' f.c", 'grep "#" f'):
        assert bash(cmd) == ALLOW, repr(cmd)


def test_hook_wrapper_fails_open_when_script_missing():
    empty = tempfile.mkdtemp()
    env = {"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": empty, "HOME": empty}
    payload = json.dumps({"tool_name": "Write", "tool_input": {}})
    proc = subprocess.run(["bash", "-c", hook_command()], input=payload, capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0 and "not found" in proc.stderr
    env["CLAUDE_PROJECT_DIR"] = ""
    assert subprocess.run(["bash", "-c", hook_command()], input=payload,
                          capture_output=True, text=True, env=env).returncode == 0


def test_hook_wrapper_finds_project_then_home_install():
    payload = json.dumps({"tool_name": "Write", "tool_input": {}})
    project = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))  # repo root
    empty = tempfile.mkdtemp()
    env = {"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": project, "HOME": empty}
    assert subprocess.run(["bash", "-c", hook_command()], input=payload,
                          capture_output=True, text=True, env=env).returncode == DENY
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".claude", "skills"))
    os.symlink(SKILL_DIR, os.path.join(home, ".claude", "skills", "tpm"))
    env.update(CLAUDE_PROJECT_DIR=empty, HOME=home)
    assert subprocess.run(["bash", "-c", hook_command()], input=payload,
                          capture_output=True, text=True, env=env).returncode == DENY


def test_off_switch_match_survives_quoting():
    for cmd in ('touch .claude/tpm.of""f', "touch .claude/tpm.of\\f", "touch '.claude/tpm.'off",
                "touch .claude/tpm .off"):
        assert bash(cmd, agent_id="a1") == DENY, cmd


def test_rg_options_that_run_programs():
    for cmd in ("rg --hostname-bin=/tmp/x foo", "rg --hostname-bin /tmp/x foo",
                "rg --pre-glob '*.gz' --pre zcat x", "rg --pre=sh x"):
        assert bash(cmd) == DENY, cmd


def test_user_invocation_only():
    assert re.search(r"^disable-model-invocation: true$", frontmatter(), re.M)


def test_quoted_operators_and_new_readers_allowed():
    for cmd in ('grep -rn "def main(" src', "grep -n '|' f", "grep -E 'a>b|c<d' f",
                "sort f | uniq -c", "uniq f", "git grep -n foo", "git rev-parse HEAD",
                "cat < f", "sed -n 1,20p f.py", "sed -n '$p' f", "sed -n 5p a b",
                "git log --format='%h (%an)'"):
        assert bash(cmd) == ALLOW, cmd
    for cmd in ("sort -o out f", "sort --output=out f", "sort -ro out f",
                "sort --compress-program=sh f", "uniq in out", "sed -n '1,5w out' f",
                "sed -n 1p -i f", "sed 1,20p f", "sed -n '/x/e rm z' f", "git grep -O vim x",
                "git grep --open-files-in-pager=vim x", "cat <<< x", "cat <(ls)",
                "cat $'\\x3b'", "ls >| f"):
        assert bash(cmd) == DENY, cmd


def test_delegation_tools_allowed_and_documented():
    for tool in ("Workflow", "mcp__claude-code-remote__create_session",
                 "mcp__claude-code-remote__send_message", "mcp__claude-code-remote__send_later",
                 "mcp__claude-code-remote__subscribe_pr_activity",
                 "mcp__claude-code-remote__unsubscribe_pr_activity"):
        assert call(tool) == ALLOW, tool
    assert call("mcp__claude-code-remote__archive_session") == DENY
    section = open(SKILL_MD).read().split("## The guard", 1)[1].split("`Bash` passes", 1)[0]
    documented = {n for n in re.findall(r"`([A-Za-z_][\w-]*)`", section)
                  if n[0].isupper() or n.startswith("mcp__")}
    assert documented == load_guard().SAFE_TOOLS, documented ^ load_guard().SAFE_TOOLS


def test_tail_follow_denied():
    for cmd in ("tail -f log", "tail -F log", "tail --follow=name log", "tail -fn 5 log", "tail --fo f", "tail -s 1 f"):
        assert bash(cmd) == DENY, cmd
    assert bash("tail -n 5 log") == ALLOW


def test_abbreviated_and_unlisted_long_options_denied():
    # Reproduced bypasses from the second review: abbreviations of dangerous options.
    for cmd in ("git grep --open=rm a", "git grep --open-files-in-pager=rm a",
                "sort --out=/tmp/pw f", "sort --compress=sh -S 1 f", "tail --fo f",
                "rg --hostname=x a", "rg --pr=sh a", "grep --unknown x f", "ls --zzz",
                "git diff --out=p", "git log --ext=x", "git show --textconv HEAD",
                "git status --unknown", "git rev-parse --git-path x", "find . -newerXY x",
                "head --foo f", "cat --zzz f", "wc --files0-from=f"):
        assert bash(cmd) == DENY, cmd


def test_uniq_counts_dash_as_a_file():
    for cmd in ("cat f | uniq - /tmp/pw", "uniq - out", "uniq -c in out", "uniq -f 2 a b"):
        assert bash(cmd) == DENY, cmd
    for cmd in ("cat f | uniq", "cat f | uniq -", "uniq -c f", "uniq -f 2 f", "sort f | uniq -c"):
        assert bash(cmd) == ALLOW, cmd


def test_common_read_options_still_allowed():
    for cmd in ("ls -la --color=auto src", "grep -rn --include='*.py' -A 3 foo src",
                "grep -C2 -e x f", "rg -n -g '*.py' --hidden foo", "sort -k2,2 -t: -rn f",
                "head -n 20 f", "tail -20 f", "wc -l f", "git log --oneline --graph -n 5",
                "git diff --stat --cached -- src", "git status --short --branch",
                "git grep -n -e foo -- '*.py'", "git rev-parse --abbrev-ref HEAD",
                "find . -maxdepth 2 -type f -name '*.md' -print"):
        assert bash(cmd) == ALLOW, cmd


def test_deny_message_points_to_light_prompts_and_scribe():
    code, err = run({"tool_name": "Write", "tool_input": {"file_path": "workstreams/x.md"}})
    assert code == DENY
    assert "one- or two-line prompt" in err and "orchestration branch" in err and "scribe" in err


def test_main_thread_cannot_write_or_push_orchestration_docs():
    # Design choice: a scribe agent owns the docs; the main thread gets no carve-out.
    assert call("Write", file_path="workstreams/finish-bag.md", content="x") == DENY
    assert call("Edit", file_path="workstreams/finish-bag.md") == DENY
    for cmd in ("git push origin orchestration", "git commit -m status",
                "git fetch origin orchestration", "git switch orchestration"):
        assert bash(cmd) == DENY, cmd
    assert bash("git show origin/orchestration:workstreams/finish-bag.md") == ALLOW
    assert call("mcp__github__get_file_contents") == ALLOW
    # The scribe is a subagent, so it can.
    assert call("Write", agent_id="scribe", file_path="workstreams/finish-bag.md") == ALLOW
    assert bash("git push origin orchestration", agent_id="scribe") == ALLOW


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
