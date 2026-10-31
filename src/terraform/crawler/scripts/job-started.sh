#!/bin/bash
# The crawler runner's pre-job hook (ACTIONS_RUNNER_HOOK_JOB_STARTED). A non-zero exit fails the job before any of
# its steps run. It accepts only a workflow_dispatch or schedule job, on an allowed ref, from an allowed workflow
# file, of the one repository. Everything else is refused: pull_request and pull_request_target jobs (fork code),
# pushes, dispatches on other branches, and any other workflow that names the `crawler` label.
# The allow list is /etc/crawler-runner/allow.env, written by root from the instance metadata (Terraform variables).
set -u

fail() {
  echo "crawler runner: refusing this job: $*" >&2
  exit 1
}

# shellcheck source=/dev/null
. /etc/crawler-runner/allow.env || fail "no allow list"

in_list() { # in_list <word> <space-separated list>
  local w
  for w in $2; do [ "$w" = "$1" ] && return 0; done
  return 1
}

repo=${GITHUB_REPOSITORY:-}
event=${GITHUB_EVENT_NAME:-}
ref=${GITHUB_REF:-}
wf_ref=${GITHUB_WORKFLOW_REF:-} # owner/repo/.github/workflows/x.yml@refs/heads/main

[ -n "$repo" ] && [ "$repo" = "$ALLOWED_REPO" ] || fail "repository '$repo'"
in_list "$event" "$ALLOWED_EVENTS" || fail "event '$event' (only: $ALLOWED_EVENTS)"
in_list "$ref" "$ALLOWED_REFS" || fail "ref '$ref' (only: $ALLOWED_REFS)"
case "$wf_ref" in
  "$ALLOWED_REPO"/*@*) ;;
  *) fail "workflow '$wf_ref' is not from $ALLOWED_REPO" ;;
esac
wf_path=${wf_ref#"$ALLOWED_REPO"/}
wf_path=${wf_path%@*}
wf_at=${wf_ref##*@}
in_list "$wf_path" "$ALLOWED_WORKFLOWS" || fail "workflow '$wf_path' (only: $ALLOWED_WORKFLOWS)"
in_list "$wf_at" "$ALLOWED_REFS" || fail "workflow file from '$wf_at'"

# A fork's event payload carries head.repo.fork; workflow_dispatch and schedule have no pull request at all.
if [ -n "${GITHUB_EVENT_PATH:-}" ] && [ -r "$GITHUB_EVENT_PATH" ] && command -v jq >/dev/null; then
  [ "$(jq -r '.pull_request // empty | length' "$GITHUB_EVENT_PATH")" = "" ] || fail "the event has a pull request"
fi

echo "crawler runner: accepted $event of $wf_path on $ref"
