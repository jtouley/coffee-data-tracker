#!/usr/bin/env bash
# Break-glass: apply src/terraform/ from Cloud Shell when GitHub is down or the pipeline is broken
# (docs/app/RUNNING.md, "Changing the infrastructure"). Run from a clean checkout of main with your terraform.tfvars:
#   bash scripts/tf_break_glass.sh
# It plans into a private temporary folder (deleted at the end: a plan holds every value in clear), runs the guard,
# shows the change list and applies that saved plan only after a yes. If the guard refuses, it applies only after you
# type `override <reason>`, which is appended, with the time, the commit and the refusing guards, to
# ~/break-glass.log. Terraform's own output never reaches the terminal; a failure shows the guard's scrubbed summary.
set -Eeuo pipefail
umask 077

TF=src/terraform
GUARD=scripts/tf_plan_guard.py
LOG="${BREAK_GLASS_LOG:-$HOME/break-glass.log}"
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
exec 3<"${ANSWERS:-/dev/tty}"  # ANSWERS only for this script's tests
die(){ printf 'STOP: %s\n' "$*" >&2; exit 1; }
ask(){ local a=""; printf '%s [y/N] ' "$*"; read -r -u 3 a || true; echo; [[ "$a" == [yY]* ]] || die "cancelled: nothing was applied"; }
tfvar(){ sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$TF/terraform.tfvars"; }
tfrun(){  # tfrun STEP ARGS...: output to a file; on failure, only the guard's scrubbed summary
  local step=$1
  shift
  if ! terraform -chdir="$TF" "$@" >"$W/$step.log" 2>&1; then
    TF_VAR_billing_account="$(tfvar billing_account)" TF_VAR_owner_email="$(tfvar owner_email)" \
      python3 -I "$GUARD" errors "$step" "$W/$step.log" >&2 || true
    die "terraform $step failed: nothing was applied (its own messages can hold private values; they are not shown)"
  fi
}

cd "$(git rev-parse --show-toplevel)"
[ -f "$TF/terraform.tfvars" ] || die "no $TF/terraform.tfvars"
[ -z "$(git status --porcelain)" ] || die "the checkout has local changes or untracked files: plan from a clean main"
tfrun init init -input=false -lockfile=readonly -no-color
tfrun plan plan -input=false -no-color -out="$W/bg.plan"
terraform -chdir="$TF" show -json -no-color "$W/bg.plan" >"$W/bg.json" 2>"$W/show.log" || die "terraform show failed"
rc=0
python3 -I "$GUARD" check "$W/bg.json" || rc=$?
[ "$rc" != 2 ] || die "the guard can't read this plan: nothing was applied"
if [ "$rc" = 1 ]; then
  echo "The guard refuses this plan (FAIL lines above). To apply it anyway, type: override <why, in a few words>"
  line=""
  read -r -u 3 line || true
  [[ "$line" =~ ^override\ .{8,}$ ]] || die "no override: nothing was applied"
  failed=$(python3 -I "$GUARD" check "$W/bg.json" | sed -n 's/^FAIL \([a-z-]*\).*/\1/p' | tr '\n' ' ' || true)
  printf '%s\tcommit %s\trefused by: %s\treason: %s\n' "$(date -u +%FT%TZ)" "$(git rev-parse HEAD)" "$failed" \
    "${line#override }" >>"$LOG"
  echo "Override logged to $LOG."
fi
python3 -I "$GUARD" render "$W/bg.json" || die "couldn't list the changes: nothing was applied"
ask "apply exactly this plan?"
tfrun apply apply -input=false -no-color "$W/bg.plan"
grep -E '^Apply complete! Resources: [0-9]+ added, [0-9]+ changed, [0-9]+ destroyed\.$' "$W/apply.log" || true
