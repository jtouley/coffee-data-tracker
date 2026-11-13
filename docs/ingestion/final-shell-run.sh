#!/usr/bin/env bash
# registry.coffee: the owner's last Cloud Shell run. After it, Terraform changes go through GitHub
# (docs/app/RUNNING.md, "Changing the infrastructure"); Cloud Shell stays only as the break-glass route.
#
# It finishes cutover 2 and sets up the GitHub route, in one go:
#   1. checks: main is checked out and has what this needs; not a Monday or a Tuesday morning; no job running
#   2. the ingest image GitHub built and signed for main's commit (verified-ingest-image.sh), tagged pinned-<commit>
#   3. plan the main root with that image, run the guard (scripts/tf_plan_guard.py), show the list, apply
#   4. verify: every job runs the pinned image; the schedules; the egress IPs
#   5. keep the pinned image in your terraform.tfvars
#   6. plan and apply the bootstrap root (src/terraform/bootstrap: the plan and apply identities, the plans bucket)
#   7. the repository variables and the two secrets the workflows need: printed (secrets never), set with gh on a yes
#   8. the first importers run, if cutover 2 hasn't started it yet
#
# Run from Cloud Shell, with the gh CLI signed in once (`gh auth login`), as:
#   bash docs/ingestion/final-shell-run.sh
# Every step asks first (y/N; anything but y stops). The first error stops the run, and nothing after it runs. Plan
# files and Terraform's output go to a private temporary folder that is deleted at the end; only error headings,
# addresses and counts are shown. No secret is printed.
set -Eeuo pipefail
umask 077

P=coffee-data-tracker; R=us-central1; TF=src/terraform; BOOT=$TF/bootstrap
GH=jtouley/coffee-data-tracker
REPO=$R-docker.pkg.dev/$P/registry-coffee/ingest
CHECKOUT="${CHECKOUT:-$HOME/coffee-data-tracker}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
trap 'printf "\033[31mSTOP: line %s failed (exit %s). Nothing after it ran.\033[0m\n" "$LINENO" "$?" >&2' ERR

# Answers come from the terminal (ANSWERS only for the script's own tests).
exec 3<"${ANSWERS:-/dev/tty}"
say(){ printf '\n\033[1m== %s  (%s UTC)\033[0m\n' "$*" "$(date -u +%T)"; }
die(){ printf '\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }
ask(){ local a=""; printf '%s [y/N] ' "$*"; read -r -u 3 a || true; echo; [[ "$a" == [yY]* ]] || die "cancelled at: $*"; }
errors(){ grep -E '^(│ )?Error: ' "$1" | sort -u | head -20 >&2 || true; }  # headings only: no values
tf(){ terraform -chdir="$1" "${@:2}"; }

say "1. checks"
ask "check out origin/main in $CHECKOUT and run the read-only checks?"
cd "$CHECKOUT"
git fetch -q origin main
git checkout -q --detach origin/main
echo "main at $(git log -1 --oneline)"
for f in "$BOOT/main.tf" scripts/tf_plan_guard.py .github/workflows/terraform-apply.yml; do
  [ -f "$f" ] || die "main has no $f: merge the Terraform-in-GitHub PRs first"
done
[ -x docs/ingestion/verified-ingest-image.sh ] || die "main has no verified-ingest-image.sh: is #153 merged?"
[ -f "$TF/terraform.tfvars" ] || die "no $TF/terraform.tfvars (your private values): copy it into this checkout first"
grep -q 'resource "google_compute_address" "woo_generic_egress"' "$TF/ingestion.tf" \
  || die "main doesn't have the second NAT IP yet: merge that PR first"
for c in jq python3 gh terraform gcloud; do command -v "$c" >/dev/null || die "$c is not installed"; done
gh auth status >/dev/null 2>&1 || die "sign the gh CLI in first: gh auth login"
# not during the crawls (Monday) or the Tuesday release (merge 03:17, enrich 04:17, build 06:17, site 11:17 UTC)
dow=$(date -u +%u); hr=$(date -u +%H)
[ "$dow" != 1 ] || die "it's Monday: crawls run today; run this Tuesday afternoon to Sunday"
if [ "$dow" = 2 ] && [ "$hr" -lt 12 ]; then die "Tuesday release still running (until ~12:00 UTC)"; fi
jobs=$(gcloud run jobs list --region $R --format='value(name)')
for j in $jobs; do
  ends=$(gcloud run jobs executions list --job "$j" --region $R --limit 5 --format='csv[no-heading](name,status.completionTime)')
  if grep -q ',$' <<<"$ends"; then die "$j has a running execution"; fi
done
echo "no job is running"
# The bootstrap gives each identity the main root's state objects only (an IAM condition), which needs uniform
# bucket-level access on the state bucket.
ubla=$(gcloud storage buckets describe "gs://$P-tfstate" --format='value(uniform_bucket_level_access)')
[[ "${ubla,,}" == true ]] \
  || die "gs://$P-tfstate doesn't have uniform bucket-level access, which the bootstrap's state conditions need"

say "2. the image GitHub built from main, verified"
ask "read and verify the ingest image for $(git rev-parse --short HEAD)?"
gcloud auth configure-docker "$R-docker.pkg.dev" --quiet >/dev/null 2>&1
IMG=$(docs/ingestion/verified-ingest-image.sh "$REPO") || IMG=""
[[ "$IMG" =~ ^"$REPO"@sha256:[0-9a-f]{64}$ ]] \
  || die "no verified image for $(git rev-parse --short HEAD) yet. GitHub builds it after ci passes on main
      (about 25 min after a merge): check https://github.com/$GH/actions/workflows/deploy.yml
      and run this again when its ingest-image job is green. Pin nothing until then."
echo "image: $IMG"
PIN="pinned-$(git rev-parse --short=12 HEAD)"
# the registry deletes images over 30 days old unless among the 5 newest, or tagged pinned-* or latest
ask "tag it $PIN, so the registry keeps it?"
gcloud artifacts docker tags add "$IMG" "$REPO:$PIN" --quiet || die "couldn't tag the image to keep it"

say "3. plan the main root with that image"
# With your terraform.tfvars only, whatever main holds in src/terraform/*.auto.tfvars (GitHub's checked-in values,
# which may still be placeholders): the root's .tf files are copied to a private folder without them, so this run
# doesn't depend on the order the PRs merge in. The copy holds no values; the state is the same bucket and prefix.
MAIN="$WORK/main-root"; TFVARS="$PWD/$TF/terraform.tfvars"
mkdir "$MAIN" && cp "$TF"/*.tf "$TF/.terraform.lock.hcl" "$MAIN/"
ask "init, check and plan $TF with your terraform.tfvars and the image above (nothing changes yet)?"
tf "$MAIN" init -input=false -lockfile=readonly -backend-config="bucket=$P-tfstate" >"$WORK/init.log" 2>&1 \
  || { errors "$WORK/init.log"; die "terraform init failed"; }
tf "$MAIN" fmt -check >/dev/null || die "terraform fmt -check fails on main"
tf "$MAIN" validate -no-color >"$WORK/validate.log" 2>&1 || { errors "$WORK/validate.log"; die "terraform validate fails"; }
tf "$MAIN" plan -input=false -no-color -var-file="$TFVARS" -var "ingest_image=$IMG" -out="$WORK/main.plan" \
  >"$WORK/plan.log" 2>&1 || { errors "$WORK/plan.log"; die "terraform plan failed"; }
tf "$MAIN" show -json "$WORK/main.plan" >"$WORK/main.json"
# no delete or replace, Shopify's network path and jobs untouched, the image pinned by digest
python3 -I scripts/tf_plan_guard.py check "$WORK/main.json" || die "the guard refuses this plan (lines above)"
echo "-- changes --"
python3 -I scripts/tf_plan_guard.py render "$WORK/main.json" || die "couldn't list the changes"
echo "expect: updates to every job (the pinned image; discover and probe commands, #132/#133); the build job's"
echo "        command and retries 1 -> 0 (#154); the job-failure alert policies' text (#154); the image registry's"
echo "        keep-pinned cleanup rule (#153); and, if not applied before: alert policies, the deploy identity's"
echo "        condition (#127), the two empty email secrets (#134), the second subnet/IP/NAT, the 21:47 retries."
echo "        Nothing may be deleted or replaced (checked above). Read the list before answering."
ask "apply exactly this plan?"
tf "$MAIN" apply -input=false -no-color "$WORK/main.plan" >"$WORK/apply.log" 2>&1 \
  || { errors "$WORK/apply.log"; die "terraform apply failed: what finished is in the state; nothing else ran"; }
grep -E '^Apply complete!' "$WORK/apply.log" || true

say "4. verify"
ask "check that every job runs $IMG and list the schedules (read-only)?"
bad=0
jobs=$(gcloud run jobs list --region $R --format='value(name)')
for j in $jobs; do
  img=$(gcloud run jobs describe "$j" --region $R --format='value(spec.template.spec.template.spec.containers[0].image)')
  if [ "$img" = "$IMG" ]; then echo "ok  $j"; else echo "!!  $j -> $img"; bad=1; fi
done
[ "$bad" = 0 ] || die "some jobs don't run the pinned image (above)"
gcloud scheduler jobs list --location $R --filter="name~registry-coffee-" --format="table(name,schedule,state)"
echo "-- egress IPs (Shopify, then WooCommerce/generic) --"
tf "$MAIN" output -raw shopify_egress_ip; echo
tf "$MAIN" output -raw woo_generic_egress_ip; echo

say "5. keep the pinned image in terraform.tfvars"
ask "write ingest_image = \"$IMG\" into $TF/terraform.tfvars?"
if grep -q '^ingest_image' "$TF/terraform.tfvars"; then
  sed -i "s|^ingest_image .*|ingest_image = \"$IMG\"|" "$TF/terraform.tfvars"
else
  echo "ingest_image = \"$IMG\"" >>"$TF/terraform.tfvars"
fi
grep -E '^ingest_image ' "$TF/terraform.tfvars"
# GitHub plans with src/terraform/ci.auto.tfvars, not this file: it must pin the same image, or the first apply
# from GitHub changes every job back.
if [ -f "$TF/ci.auto.tfvars" ] && ! grep -qF "\"$IMG\"" "$TF/ci.auto.tfvars"; then
  echo "NOTE: $TF/ci.auto.tfvars pins a different image. Open a one-line PR setting its ingest_image to"
  echo "      $IMG"
  echo "      before the next Terraform change, or GitHub's plan will show every job changing back."
fi

say "6. the bootstrap root: identities for GitHub's plan and apply, and the plans bucket"
BILLING=$(sed -n 's/^[[:space:]]*billing_account[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$TF/terraform.tfvars")
[[ "$BILLING" =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]] || die "no billing_account in $TF/terraform.tfvars (value not shown)"
export TF_VAR_billing_account="$BILLING"
ask "init, check and plan $BOOT (nothing changes yet)?"
tf $BOOT init -input=false -lockfile=readonly -backend-config="bucket=$P-tfstate" >"$WORK/boot-init.log" 2>&1 \
  || { errors "$WORK/boot-init.log"; die "terraform init failed (bootstrap)"; }
tf $BOOT fmt -check >/dev/null || die "terraform fmt -check fails on the bootstrap"
tf $BOOT validate -no-color >"$WORK/boot-validate.log" 2>&1 || { errors "$WORK/boot-validate.log"; die "terraform validate fails (bootstrap)"; }
tf $BOOT plan -input=false -no-color -out="$WORK/boot.plan" >"$WORK/boot-plan.log" 2>&1 \
  || { errors "$WORK/boot-plan.log"; die "terraform plan failed (bootstrap)"; }
tf $BOOT show -json "$WORK/boot.plan" >"$WORK/boot.json"
BAD=$(jq -r '.resource_changes[]? | select(.change.actions | index("delete")) | .address' "$WORK/boot.json")
[ -z "$BAD" ] || die "the bootstrap plan deletes or replaces: $BAD"
echo "-- changes --"
jq -r '.resource_changes[]? | select(.change.actions != ["no-op"] and .change.actions != ["read"])
  | "\(.change.actions | join("+"))\t\(.address)"' "$WORK/boot.json" | sort
echo "expect (first run): 50 creates: two pools and providers, two accounts and their workload identity bindings,"
echo "        five custom roles, the plans bucket, and the two accounts' role bindings. Nothing else."
ask "apply exactly this plan?"
tf $BOOT apply -input=false -no-color "$WORK/boot.plan" >"$WORK/boot-apply.log" 2>&1 \
  || { errors "$WORK/boot-apply.log"; die "terraform apply failed (bootstrap): what finished is in its state; nothing else ran"; }
grep -E '^Apply complete!' "$WORK/boot-apply.log" || true

say "7. GitHub repository variables and secrets"
tf $BOOT output -json github_variables >"$WORK/vars.json"
echo "repository variables (none is a secret):"
jq -r 'to_entries[] | "  \(.key) = \(.value)"' "$WORK/vars.json"
ask "set these as repository variables on $GH with gh?"
while IFS=$'\t' read -r name value; do
  gh variable set "$name" -R "$GH" --body "$value" >/dev/null
  echo "set $name"
done < <(jq -r 'to_entries[] | "\(.key)\t\(.value)"' "$WORK/vars.json")
# The private values, as repository secrets: read from terraform.tfvars, piped to gh, never shown or written anywhere.
tfvar(){ sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$TF/terraform.tfvars"; }
SECRETS="billing_account:TF_VAR_BILLING_ACCOUNT owner_email:TF_VAR_OWNER_EMAIL"
for pair in $SECRETS; do [ -n "$(tfvar "${pair%%:*}")" ] || die "no ${pair%%:*} in $TF/terraform.tfvars"; done
names=$(for pair in $SECRETS; do printf '%s ' "${pair#*:}"; done)
ask "set the repository secrets ${names% } from terraform.tfvars (values not shown)?"
for pair in $SECRETS; do
  gh secret set "${pair#*:}" -R "$GH" < <(tfvar "${pair%%:*}" | tr -d '\n') >/dev/null
  echo "set ${pair#*:}"
done
echo "your other terraform.tfvars settings, for the FILL_IN values of $TF/ci.auto.tfvars (none is a secret):"
grep -vE '^[[:space:]]*(#|$)' "$TF/terraform.tfvars" | grep -vE '^[[:space:]]*(billing_account|owner_email)[[:space:]]*=' \
  | sed 's/^/  /' || true

say "8. first importers run (skip it if cutover 2 already started one)"
ask "run importers.yml on main (importer=all; a manual run never fetches Royal)?"
gh workflow run importers.yml -R "$GH" --ref main -f importer=all
echo "started: https://github.com/$GH/actions/workflows/importers.yml"

say "DONE. Next: file the SiteGround ticket with the WooCommerce/generic IP above. From now on, Terraform changes are PRs:"
echo "      merge, then approve the terraform-apply run as jtouleyrou (docs/app/RUNNING.md, \"Changing the infrastructure\")."
