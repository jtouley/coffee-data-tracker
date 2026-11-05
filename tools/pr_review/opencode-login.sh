#!/usr/bin/env bash
# Exchanges this GitHub Actions job's OIDC token for a short-lived OpenCode API key and prints OpenCode's JSON
# response ({"access_token": ..., "expires_in": ...}). Needs `permissions: id-token: write` and OPENCODE_CLIENT_ID.
# pr_review.py calls it, and again before the key expires.
set -euo pipefail
audience=https://opencode.ai/console

oidc=$(curl -sSf --oauth2-bearer "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=$audience" | jq -r .value) \
  || { echo "GitHub refused the OIDC token request (is id-token: write set?)" >&2; exit 1; }

curl -sS --fail-with-body https://opencode.ai/console/auth/oauth/token \
  -d grant_type=urn:ietf:params:oauth:grant-type:token-exchange \
  -d subject_token_type=urn:ietf:params:oauth:token-type:jwt \
  -d client_id="$OPENCODE_CLIENT_ID" \
  -d subject_token="$oidc" \
  || { echo >&2; echo "OpenCode refused the token exchange (response above)" >&2; exit 1; }
