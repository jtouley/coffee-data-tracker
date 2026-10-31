#!/bin/bash
# The crawler VM's startup script: runs as root on every boot (Terraform puts it in the instance metadata).
#  - a 2 GB swap file, so a memory peak slows the run instead of killing it;
#  - an unprivileged `runner` user: no sudo, no password, no docker group;
#  - jobs (anything run as `runner`) can't reach the metadata server, so they can't use the VM's service account;
#  - on first boot only: the pinned actions/runner release (checksum verified), registered with the one-hour
#    registration token from Secret Manager, labels self-hosted, Linux, X64, crawler;
#  - a root-owned systemd unit runs it, with root-owned pre- and post-job hooks. The pre-job hook refuses every job
#    that isn't workflow_dispatch or schedule, on an allowed ref, from an allowed workflow of this repository.
# Logs: `sudo journalctl -u google-startup-scripts` on the VM.
set -euo pipefail

md() { curl -fsS -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }
attr() { md "instance/attributes/$1"; }

REPO=$(attr crawler-repo)
NAME=$(attr crawler-runner-name)
VERSION=$(attr crawler-runner-version)
SHA256=$(attr crawler-runner-sha256)
SECRET=$(attr crawler-token-secret)
PROJECT=$(md project/project-id)
DIR=/opt/actions-runner
LIB=/usr/local/lib/crawler-runner

# --- swap
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
grep -q '^/swapfile ' /proc/swaps || swapon /swapfile

# --- packages (Ubuntu's unattended-upgrades stays on for security updates)
if ! command -v jq >/dev/null || ! command -v git >/dev/null || ! command -v iptables >/dev/null; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -yq curl jq git iptables ca-certificates
fi

# --- the unprivileged user
if ! id runner >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash runner
  passwd --lock runner
fi
gpasswd -d runner sudo >/dev/null 2>&1 || true

# --- hooks and the allow list: root-owned, read-only to the runner
install -d -m 0755 -o root -g root "$LIB" /etc/crawler-runner
attr crawler-job-started > "$LIB/job-started.sh"
attr crawler-job-completed > "$LIB/job-completed.sh"
chmod 0755 "$LIB/job-started.sh" "$LIB/job-completed.sh"
{
  printf 'ALLOWED_REPO=%q\n' "$REPO"
  printf 'ALLOWED_EVENTS=%q\n' "$(attr crawler-allowed-events)"
  printf 'ALLOWED_REFS=%q\n' "$(attr crawler-allowed-refs)"
  printf 'ALLOWED_WORKFLOWS=%q\n' "$(attr crawler-allowed-wfs)"
} > /etc/crawler-runner/allow.env
chmod 0644 /etc/crawler-runner/allow.env

# --- jobs can't reach the metadata server's HTTP API (DNS on 169.254.169.254:53 still works)
cat > "$LIB/firewall.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
uid=$(id -u runner)
for port in 80 8080; do
  iptables -C OUTPUT -p tcp -d 169.254.169.254 --dport "$port" -m owner --uid-owner "$uid" -j REJECT 2>/dev/null ||
    iptables -I OUTPUT -p tcp -d 169.254.169.254 --dport "$port" -m owner --uid-owner "$uid" -j REJECT
done
EOF
chmod 0755 "$LIB/firewall.sh"

# --- install and register, once
if [ ! -f "$DIR/.runner" ]; then
  install -d -m 0755 -o runner -g runner "$DIR"
  tarball="actions-runner-linux-x64-${VERSION}.tar.gz"
  curl -fsSL -o "/tmp/$tarball" "https://github.com/actions/runner/releases/download/v${VERSION}/${tarball}"
  echo "${SHA256}  /tmp/$tarball" | sha256sum -c -
  sudo -u runner -H tar xzf "/tmp/$tarball" -C "$DIR"
  rm -f "/tmp/$tarball"
  "$DIR/bin/installdependencies.sh"

  access_token=$(md instance/service-accounts/default/token | jq -r .access_token)
  token=$(curl -fsS -H "Authorization: Bearer $access_token" \
    "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$SECRET/versions/latest:access" |
    jq -r .payload.data | base64 -d | tr -d '[:space:]') || token=""
  if [ -z "$token" ]; then
    echo "crawler: no registration token in secret $SECRET; add one and run: sudo google_metadata_script_runner startup" >&2
    exit 1
  fi
  if ! sudo -u runner -H "$DIR/config.sh" --unattended --replace --url "https://github.com/$REPO" --token "$token" \
    --name "$NAME" --labels crawler --work _work; then
    echo "crawler: registration failed (registration tokens expire after an hour): add a fresh one and run: sudo google_metadata_script_runner startup" >&2
    exit 1
  fi
  unset token access_token
fi

# --- the service: root-owned unit, hooks set here where a job can't change them
cat > /etc/systemd/system/actions-runner.service <<EOF
[Unit]
Description=GitHub Actions runner ($NAME, label crawler)
After=network-online.target
Wants=network-online.target

[Service]
User=runner
Group=runner
WorkingDirectory=$DIR
ExecStartPre=+$LIB/firewall.sh
ExecStart=$DIR/run.sh
Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED=$LIB/job-started.sh
Environment=ACTIONS_RUNNER_HOOK_JOB_COMPLETED=$LIB/job-completed.sh
Restart=always
RestartSec=10
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable actions-runner.service
systemctl restart actions-runner.service
echo "crawler: runner $NAME running for $REPO"
