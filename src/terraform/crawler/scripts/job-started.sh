#!/bin/bash
# crawler-runner-hook
# The crawler runner's pre-job hook (ACTIONS_RUNNER_HOOK_JOB_STARTED). A non-zero exit fails the job before its
# first step. It accepts only a workflow_dispatch or schedule job on main, from an allowed workflow file of this
# repository: pull_request and pull_request_target jobs (fork code), pushes, other branches and any other workflow
# that names the `crawler` label are refused. The line above and the last line are the markers the startup script
# and the runner's unit check, so an empty or cut-off copy is never installed or run.
# To allow weekly.yml later, add it to WORKFLOWS and re-run the startup script.
set -u
REPO="jtouley/coffee-data-tracker"
EVENTS="workflow_dispatch schedule"
REF="refs/heads/main"
WORKFLOWS=".github/workflows/live-test.yml"

fail() {
  echo "crawler runner: refusing this job: $*" >&2
  exit 1
}
in_list() { # in_list <word> <space-separated list>
  local w
  for w in $2; do [ "$w" = "$1" ] && return 0; done
  return 1
}

repo=${GITHUB_REPOSITORY:-}
event=${GITHUB_EVENT_NAME:-}
ref=${GITHUB_REF:-}
wf_ref=${GITHUB_WORKFLOW_REF:-} # owner/repo/.github/workflows/x.yml@refs/heads/main

[ "$repo" = "$REPO" ] || fail "repository '$repo'"
in_list "$event" "$EVENTS" || fail "event '$event' (only: $EVENTS)"
[ "$ref" = "$REF" ] || fail "ref '$ref' (only: $REF)"
case "$wf_ref" in
  "$REPO"/*@"$REF") ;;
  *) fail "workflow '$wf_ref'" ;;
esac
wf_path=${wf_ref#"$REPO"/}
wf_path=${wf_path%@*}
in_list "$wf_path" "$WORKFLOWS" || fail "workflow '$wf_path' (only: $WORKFLOWS)"

echo "crawler runner: accepted $event of $wf_path on $ref"
# crawler-runner-hook end
