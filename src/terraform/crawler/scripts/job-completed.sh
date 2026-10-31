#!/bin/bash
# The crawler runner's post-job hook (ACTIONS_RUNNER_HOOK_JOB_COMPLETED): no checkout, lake copy or cache from one
# job is left for the next. The runner's tool cache (_work/_tool: Python, uv) stays; it holds no data.
set -u
ws=${GITHUB_WORKSPACE:-}
repo_dir=$(dirname "$ws")
case "$repo_dir" in
  /opt/actions-runner/_work/_* | /opt/actions-runner/_work) ;; # never the runner's own folders
  /opt/actions-runner/_work/*) rm -rf "$repo_dir" ;;
esac
rm -rf /opt/actions-runner/_work/_temp/* "$HOME/.cache/huggingface" 2>/dev/null || true
exit 0
