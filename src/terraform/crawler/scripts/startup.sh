#!/bin/bash
# The crawler VM's startup script: runs as root on every boot (Terraform puts it in the instance metadata).
#  - a 2 GB swap file; an unprivileged `runner` user (no sudo, no docker, locked password);
#  - jobs (anything run as `runner`) can reach the metadata server only for DNS, so not the VM's service account;
#  - on first boot: the pinned actions/runner (checksum verified) in a root-owned /opt/actions-runner, registered
#    with the one-hour token from Secret Manager, label crawler, self-update off;
#  - the pre-job hook, root-owned, installed only if complete; the runner's unit won't start without it.
# Logs: `sudo journalctl -u google-startup-scripts` on the VM.
set -euo pipefail

DIR=/opt/actions-runner
LIB=/usr/local/lib/crawler-runner
HOOK=$LIB/job-started.sh
MARKER="# crawler-runner-hook"

md() { curl -fsS -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
attr() { md "instance/attributes/$1"; }

# hook_ok FILE: non-empty, has the first marker line, ends with the end marker (so not cut off), parses. The unit's
# ExecStartPre runs the same test.
hook_ok() { [ -s "$1" ] && grep -qxF "$MARKER" "$1" && [ "$(tail -n 1 "$1")" = "$MARKER end" ] && bash -n "$1"; }

# install_hook DEST: fetch to a temp file beside DEST, check it, then rename it into place. A failed fetch leaves
# the previous hook (or none, and then the runner doesn't start), never an empty one.
install_hook() {
  local dest=$1 tmp
  tmp=$(mktemp "$dest.XXXXXX")
  if attr crawler-job-started > "$tmp" && hook_ok "$tmp"; then
    chmod 0755 "$tmp"
    mv -f "$tmp" "$dest"
  else
    rm -f "$tmp"
    echo "crawler: the pre-job hook from metadata is missing or incomplete; keeping the old one" >&2
    return 1
  fi
}

block_metadata() { # only DNS (53) to 169.254.169.254 for the runner user
  local uid
  uid=$(id -u runner)
  iptables -N CRAWLER-META 2>/dev/null || iptables -F CRAWLER-META
  iptables -A CRAWLER-META -p udp --dport 53 -j RETURN
  iptables -A CRAWLER-META -p tcp --dport 53 -j RETURN
  iptables -A CRAWLER-META -j REJECT
  iptables -C OUTPUT -d 169.254.169.254 -m owner --uid-owner "$uid" -j CRAWLER-META 2>/dev/null ||
    iptables -I OUTPUT -d 169.254.169.254 -m owner --uid-owner "$uid" -j CRAWLER-META
}

# fn_script FILE COMMAND FUNCTION...: a root-owned script with these functions that runs COMMAND.
fn_script() {
  local file=$1 cmd=$2
  shift 2
  {
    printf '#!/bin/bash\nset -euo pipefail\nMARKER=%q\n' "$MARKER"
    declare -f "$@"
    printf '%s\n' "$cmd"
  } > "$file.tmp"
  chmod 0755 "$file.tmp"
  mv -f "$file.tmp" "$file"
}

register() {
  local repo name version sha secret project tarball access_token token
  repo=$(attr crawler-repo)
  name=$(attr crawler-runner-name)
  version=$(attr crawler-runner-version)
  sha=$(attr crawler-runner-sha256)
  secret=$(attr crawler-token-secret)
  project=$(md project/project-id)
  tarball="actions-runner-linux-x64-${version}.tar.gz"
  install -d -m 0755 -o root -g root "$DIR"
  curl -fsSL -o "/tmp/$tarball" "https://github.com/actions/runner/releases/download/v${version}/${tarball}"
  echo "${sha}  /tmp/$tarball" | sha256sum -c -
  tar xzf "/tmp/$tarball" -C "$DIR"
  rm -f "/tmp/$tarball"
  "$DIR/bin/installdependencies.sh"

  access_token=$(md instance/service-accounts/default/token | jq -r .access_token)
  token=$(curl -fsS -H "Authorization: Bearer $access_token" \
    "https://secretmanager.googleapis.com/v1/projects/$project/secrets/$secret/versions/latest:access" |
    jq -r .payload.data | base64 -d | tr -d '[:space:]') || token=""
  if [ -z "$token" ]; then
    echo "crawler: no token in secret $secret; add one, then: sudo google_metadata_script_runner startup" >&2
    return 1
  fi
  # config.sh refuses root unless told; it writes .runner and the credentials, all left root-owned below.
  if ! RUNNER_ALLOW_RUNASROOT=1 "$DIR/config.sh" --unattended --replace --disableupdate \
    --url "https://github.com/$repo" --token "$token" --name "$name" --labels crawler --work _work; then
    echo "crawler: registration failed (tokens expire after an hour); add a fresh one, then: sudo google_metadata_script_runner startup" >&2
    return 1
  fi
}

main() {
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  grep -q '^/swapfile ' /proc/swaps || swapon /swapfile

  if ! command -v jq >/dev/null || ! command -v git >/dev/null || ! command -v iptables >/dev/null; then
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -yq curl jq git iptables ca-certificates
  fi

  if ! id runner >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash runner
    passwd --lock runner
  fi
  gpasswd -d runner sudo >/dev/null 2>&1 || true

  install -d -m 0755 -o root -g root "$LIB"
  install_hook "$HOOK" || true # the unit's ExecStartPre refuses to start without a good hook
  block_metadata

  [ -f "$DIR/.runner" ] || register

  # Root owns the install, its config and the .env the runner loads; the job user writes only _work and _diag.
  { grep -v '^ACTIONS_RUNNER_HOOK' "$DIR/.env" 2>/dev/null || true; echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=$HOOK"; } > "$DIR/.env.tmp"
  mv -f "$DIR/.env.tmp" "$DIR/.env"
  chown -R root:root "$DIR"
  chmod -R go-w "$DIR"
  for f in .runner .credentials .credentials_rsaparams; do
    [ -e "$DIR/$f" ] && chgrp runner "$DIR/$f" && chmod 0640 "$DIR/$f"
  done
  install -d -m 0750 -o runner -g runner "$DIR/_work" "$DIR/_diag"
  # Root-owned helpers the unit runs: the hook check (as the runner user) and the metadata block (as root).
  fn_script "$LIB/check-hook.sh" "hook_ok $HOOK" hook_ok
  fn_script "$LIB/firewall.sh" block_metadata block_metadata

  cat > /etc/systemd/system/actions-runner.service <<EOF
[Unit]
Description=GitHub Actions runner (label crawler)
After=network-online.target
Wants=network-online.target

[Service]
User=runner
Group=runner
WorkingDirectory=$DIR
ExecStartPre=$LIB/check-hook.sh
ExecStartPre=+$LIB/firewall.sh
ExecStart=$DIR/bin/Runner.Listener run --startuptype service
Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED=$HOOK
Restart=always
RestartSec=10
KillMode=control-group
TimeoutStopSec=5min
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadOnlyPaths=$DIR
ReadWritePaths=$DIR/_work $DIR/_diag
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable actions-runner.service
  systemctl restart actions-runner.service
  echo "crawler: runner running"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
