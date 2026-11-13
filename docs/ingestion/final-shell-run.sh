#!/usr/bin/env bash
# registry.coffee: the owner's last Cloud Shell run. After it, Terraform changes go through GitHub
# (docs/app/RUNNING.md, "Changing the infrastructure"); Cloud Shell stays only as the break-glass route.
#
# It finishes cutover 2 and sets up the GitHub route, in one go:
#   1. checks: main is checked out and has what this needs; the weekly pipeline isn't due; no job running; the state
#      bucket has uniform access and versioning
#   2. the ingest image GitHub built and signed for main's commit (verified-ingest-image.sh), tagged pinned-<commit>
#   3. plan the main root with your terraform.tfvars and that image, run the guard, show the list, apply
#   4. verify: every job runs the pinned image; the schedules; the two egress IPs (for the SiteGround ticket)
#   5. keep the pinned image in your terraform.tfvars
#   6. plan and apply the bootstrap root (src/terraform/bootstrap: the plan and apply identities, the plans bucket)
#   7. the repository variables and secrets the workflows need, set with gh (names shown, values never)
#   8. the first importers run, if cutover 2 hasn't started it yet
#
# Run from Cloud Shell, with the gh CLI signed in once (`gh auth login`), as:
#   bash docs/ingestion/final-shell-run.sh
# Every step asks first (y/N; anything but y stops). The first error stops the run, and nothing after it runs.
# Terraform's own output never reaches the terminal: it goes to files in a private temporary folder (deleted at the
# end, with the saved plans, which hold every value in clear); a failure shows the guard's fixed, scrubbed summary.
# No secret value is printed, passed as an argument or written anywhere but that folder.
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
# A string value from terraform.tfvars, for scrubbing and for gh's stdin only: never printed.
tfvar(){ sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$TF/terraform.tfvars"; }
# tfrun STEP DIR ARGS...: Terraform with its output in a file; on failure, only the guard's scrubbed summary.
tfrun(){
  local step=$1 dir=$2
  shift 2
  if ! terraform -chdir="$dir" "$@" >"$WORK/$step.log" 2>&1; then
    TF_VAR_billing_account="$(tfvar billing_account)" TF_VAR_owner_email="$(tfvar owner_email)" \
      python3 -I scripts/tf_plan_guard.py errors "$step" "$WORK/$step.log" >&2 || true
    die "terraform $step failed in $dir (its own messages are not shown: they can hold private values)"
  fi
}
# The weekly pipeline (src/terraform/ingestion.tf): crawls and fetch-health on Monday, fetch-health and the release
# on Tuesday, discover on the 1st at 03:17, all UTC.
not_now(){
  local dow hr dom
  dow=$(date -u +%u); hr=$((10#$(date -u +%H))); dom=$(date -u +%d)
  if [ "$dow" = 1 ] || [ "$dow" = 2 ] || { [ "$dow" = 7 ] && [ "$hr" -ge 23 ]; } || { [ "$dom" = 01 ] && [ "$hr" -ge 2 ] && [ "$hr" -le 4 ]; }; then
    die "the weekly pipeline runs now (Monday and Tuesday UTC, from Sunday 23:00, and 02:00-05:00 on the 1st): run this Wednesday to Sunday"
  fi
  local jobs j ends
  jobs=$(gcloud run jobs list --project $P --region $R --format='value(name)')
  for j in $jobs; do
    ends=$(gcloud run jobs executions list --project $P --job "$j" --region $R --limit 5 --format='csv[no-heading](name,status.completionTime)')
    if grep -q ',$' <<<"$ends"; then die "$j has a running execution"; fi
  done
}

say "1. checks"
ask "check out origin/main in $CHECKOUT and run the read-only checks?"
cd "$CHECKOUT"
git fetch -q origin main
git checkout -q --detach origin/main
echo "main at $(git log -1 --oneline)"
# local edits or untracked .tf files would be planned and applied too (terraform.tfvars is ignored, so it's fine)
[ -z "$(git status --porcelain)" ] || die "the checkout has local changes or untracked files: clean it first (git status)"
# Terraform loads every *.tf, *.tf.json and *.auto.tfvars(.json) in a root, gitignored or not: refuse any but your
# terraform.tfvars, so only main's configuration is planned.
loaded=$(git status --porcelain --ignored -- src/terraform \
  | grep -E '^!! src/terraform/(bootstrap/)?[^/]+\.(tf|tf\.json|tfvars|tfvars\.json)$' \
  | grep -vx '!! src/terraform/terraform.tfvars' || true)
[ -z "$loaded" ] || die "ignored files Terraform would load: ${loaded//'!! '/}"
for f in "$BOOT/main.tf" scripts/tf_plan_guard.py .github/workflows/terraform-apply.yml "$TF/ci.tfvars"; do
  [ -f "$f" ] || die "main has no $f: merge the Terraform-in-GitHub PRs first"
done
[ -x docs/ingestion/verified-ingest-image.sh ] || die "main has no verified-ingest-image.sh: is #153 merged?"
[ -f "$TF/terraform.tfvars" ] || die "no $TF/terraform.tfvars (your private values): copy it into this checkout first"
for v in billing_account owner_email; do [ -n "$(tfvar $v)" ] || die "$TF/terraform.tfvars has no $v"; done
grep -q 'resource "google_compute_address" "woo_generic_egress"' "$TF/ingestion.tf" \
  || die "main doesn't have the second NAT IP yet: merge that PR first"
for c in jq python3 gh terraform gcloud; do command -v "$c" >/dev/null || die "$c is not installed"; done
gh auth status >/dev/null 2>&1 || die "sign the gh CLI in first: gh auth login"
not_now
echo "no weekly job is due or running"
# The bootstrap gives each identity the main root's state objects only (an IAM condition), which needs uniform
# bucket-level access; versioning keeps every earlier state, should an apply go wrong.
ubla=$(gcloud storage buckets describe "gs://$P-tfstate" --project $P --format='value(uniform_bucket_level_access)')
[[ "${ubla,,}" == true ]] || die "gs://$P-tfstate doesn't have uniform bucket-level access, which the bootstrap's state conditions need"
versioning=$(gcloud storage buckets describe "gs://$P-tfstate" --project $P --format='value(versioning_enabled)')
[[ "${versioning,,}" == true ]] || die "gs://$P-tfstate has no object versioning: turn it on first (gcloud storage buckets update gs://$P-tfstate --versioning)"

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
gcloud artifacts docker tags add "$IMG" "$REPO:$PIN" --quiet >/dev/null 2>&1 || die "couldn't tag the image to keep it"

say "3. plan the main root with that image"
# Your terraform.tfvars only, and never GitHub's ci.tfvars or any *.auto.tfvars: the root's .tf files are copied to
# the private folder, so this run plans the same whatever order the PRs merged in. The state is the same.
MAIN="$WORK/main-root"; TFVARS="$PWD/$TF/terraform.tfvars"
mkdir "$MAIN" && cp "$TF"/*.tf "$TF/.terraform.lock.hcl" "$MAIN/"
ask "init, check and plan $TF with your terraform.tfvars and the image above (nothing changes yet)?"
tfrun init "$MAIN" init -input=false -lockfile=readonly -no-color -backend-config="bucket=$P-tfstate"
tfrun fmt "$MAIN" fmt -check -no-color
tfrun validate "$MAIN" validate -no-color
tfrun plan "$MAIN" plan -input=false -no-color -var-file="$TFVARS" -var "ingest_image=$IMG" -out="$WORK/main.plan"
terraform -chdir="$MAIN" show -json "$WORK/main.plan" >"$WORK/main.json" 2>"$WORK/show.log" || die "terraform show failed"
echo "-- guard --"
rc=0
python3 -I scripts/tf_plan_guard.py check "$WORK/main.json" || rc=$?
[ "$rc" != 2 ] || die "the guard can't read this plan"
if [ "$rc" = 1 ]; then
  # Two guards protect what only you change, here: who can deploy, plan or apply. And cutover 2 may still create the
  # WooCommerce/generic NAT on the ingest router, which shopify-network-path refuses in CI: that one creation, alone,
  # is yours to confirm too. Anything else stops the run.
  out=$(python3 -I scripts/tf_plan_guard.py check "$WORK/main.json" || true)
  other=$(sed -n 's/^FAIL \([a-z-]*\).*/\1/p' <<<"$out" | grep -vxE 'workload-identity|guarded-accounts' || true)
  woo=google_compute_router_nat.woo_generic_egress
  if [ "$other" = shopify-network-path ] && [ "$(sed -n 's/^FAIL shopify-network-path: //p' <<<"$out")" = "$woo" ] \
    && jq -e --arg a "$woo" '[.resource_changes[] | select(.address == $a) | .change.actions] == [["create"]]' \
      "$WORK/main.json" >/dev/null; then
    other=""
  fi
  [ -z "$other" ] || die "the guard refuses this plan: $(echo "$other" | tr '\n' ' ')"
  echo "The FAIL lines above change who can deploy, plan or apply, or create the WooCommerce/generic NAT."
  echo "Only you change these, here."
  ask "they are expected (such as the deploy identity's condition, #127, or cutover 2's second NAT): go on?"
fi
echo "-- changes --"
python3 -I scripts/tf_plan_guard.py render "$WORK/main.json" || die "couldn't list the changes"
echo "expect: updates to every job (the pinned image; discover and probe commands, #132/#133); the build job's"
echo "        command and retries 1 -> 0 (#154); the job-failure alert policies' text (#154); the image registry's"
echo "        keep-pinned cleanup rule (#153); and, if not applied before: alert policies, the deploy identity's"
echo "        condition (#127), the two empty email secrets (#134), the second subnet/IP/NAT, the 21:47 retries."
echo "        Nothing may be deleted or replaced (checked above). Read the list before answering."
ask "apply exactly this plan?"
not_now
tfrun apply "$MAIN" apply -input=false -no-color "$WORK/main.plan"
grep -E '^Apply complete! Resources: [0-9]+ added, [0-9]+ changed, [0-9]+ destroyed\.$' "$WORK/apply.log" || true

say "4. verify"
ask "check that every job runs $IMG, list the schedules and show the egress IPs (read-only)?"
bad=0
jobs=$(gcloud run jobs list --project $P --region $R --format='value(name)')
[ -n "$jobs" ] || die "no jobs listed"
for j in $jobs; do
  img=$(gcloud run jobs describe "$j" --project $P --region $R --format='value(spec.template.spec.template.spec.containers[0].image)')
  if [ "$img" = "$IMG" ]; then echo "ok  $j"; else echo "!!  $j -> $img"; bad=1; fi
done
[ "$bad" = 0 ] || die "some jobs don't run the pinned image (above)"
gcloud scheduler jobs list --project $P --location $R --filter="name~registry-coffee-" --format="table(name,schedule,state)"
echo "-- egress IPs, for the SiteGround ticket (Shopify, then WooCommerce/generic) --"
for a in shopify-egress woo-generic-egress; do
  gcloud compute addresses describe "registry-coffee-$a" --project $P --region $R --format='value(address)'
done

say "5. keep the pinned image in terraform.tfvars"
ask "write ingest_image = \"$IMG\" into $TF/terraform.tfvars?"
IMG_LINE='^[[:space:]]*ingest_image[[:space:]]*='
if grep -qE "$IMG_LINE" "$TF/terraform.tfvars"; then
  sed -i -E "s|$IMG_LINE.*|ingest_image = \"$IMG\"|" "$TF/terraform.tfvars"
else
  echo "ingest_image = \"$IMG\"" >>"$TF/terraform.tfvars"
fi
[ "$(grep -cE "$IMG_LINE" "$TF/terraform.tfvars")" = 1 ] || die "terraform.tfvars should have one ingest_image line"
grep -E "$IMG_LINE" "$TF/terraform.tfvars"

say "6. the bootstrap root: identities for GitHub's plan and apply, and the plans bucket"
TF_VAR_billing_account="$(tfvar billing_account)"
export TF_VAR_billing_account
ask "init, check and plan $BOOT (nothing changes yet)?"
tfrun init "$BOOT" init -input=false -lockfile=readonly -no-color -backend-config="bucket=$P-tfstate"
tfrun fmt "$BOOT" fmt -check -no-color
tfrun validate "$BOOT" validate -no-color
tfrun plan "$BOOT" plan -input=false -no-color -out="$WORK/boot.plan"
terraform -chdir="$BOOT" show -json "$WORK/boot.plan" >"$WORK/boot.json" 2>"$WORK/show.log" || die "terraform show failed (bootstrap)"
BAD=$(jq -r '.resource_changes[]? | select(.change.actions | index("delete")) | .address' "$WORK/boot.json")
[ -z "$BAD" ] || die "the bootstrap plan deletes or replaces: $BAD"
echo "-- changes (addresses only) --"
jq -r '.resource_changes[]? | select(.change.actions != ["no-op"] and .change.actions != ["read"])
  | "\(.change.actions | join("+"))\t\(.address)"' "$WORK/boot.json" | sort
echo "expect (first run): 51 creates: two pools and providers, two accounts and their workload identity bindings,"
echo "        seven custom roles, the plans bucket, and the two accounts' role bindings. Nothing else."
ask "apply exactly this plan?"
tfrun apply "$BOOT" apply -input=false -no-color "$WORK/boot.plan"
grep -E '^Apply complete! Resources: [0-9]+ added, [0-9]+ changed, [0-9]+ destroyed\.$' "$WORK/apply.log" || true
unset TF_VAR_billing_account

say "7. GitHub repository variables and secrets"
terraform -chdir="$BOOT" output -json github_variables >"$WORK/vars.json" 2>"$WORK/output.log" || die "terraform output failed (bootstrap)"
# Your values, as this run's plan used them (terraform.tfvars, the variables' defaults, the pinned image).
val(){ jq -c --arg n "$1" '.variables[$n].value' "$WORK/main.json"; }
raw(){ jq -j --arg n "$1" '.variables[$n].value' "$WORK/main.json"; }
sets(){ grep -qE "^[[:space:]]*$1[[:space:]]*=" "$TF/terraform.tfvars"; }
{
  jq -r 'to_entries[] | "\(.key)\t\(.value)"' "$WORK/vars.json"
  printf 'TF_VAR_INGEST_IMAGE\t%s\n' "$IMG"
  printf 'TF_VAR_INGEST_SCHEDULE_ENABLED\t%s\n' "$(raw ingest_schedule_enabled)"
  if sets email; then printf 'TF_VAR_EMAIL\t%s\n' "$(raw email)"; fi
  if sets budget_usd; then printf 'TF_VAR_BUDGET_USD\t%s\n' "$(raw budget_usd)"; fi
  if sets ingest_job_memory; then printf 'TF_VAR_INGEST_JOB_MEMORY\t%s\n' "$(val ingest_job_memory)"; fi
} >"$WORK/gh-vars.tsv"
echo "repository variables to set: $(cut -f1 "$WORK/gh-vars.tsv" | tr '\n' ' ')"
ask "set these repository variables on $GH with gh (values not shown)?"
while IFS=$'\t' read -r name value; do
  gh variable set "$name" -R "$GH" --body "$value" >/dev/null
  echo "set $name"
done <"$WORK/gh-vars.tsv"
ask "set the repository secrets TF_VAR_BILLING_ACCOUNT and TF_VAR_OWNER_EMAIL from terraform.tfvars (values not shown)?"
for pair in billing_account:TF_VAR_BILLING_ACCOUNT owner_email:TF_VAR_OWNER_EMAIL; do
  gh secret set "${pair#*:}" -R "$GH" < <(raw "${pair%%:*}") >/dev/null
  echo "set ${pair#*:}"
done
# What GitHub's plan won't see: a setting in terraform.tfvars that no variable above or ci.tfvars carries.
known='billing_account|owner_email|ingest_image|ingest_schedule_enabled|email|budget_usd|ingest_job_memory|verdict_model'
left=$(sed -n -E 's/^[[:space:]]*([a-z_]+)[[:space:]]*=.*/\1/p' "$TF/terraform.tfvars" | grep -vxE "$known" | sort -u | tr '\n' ' ' || true)
[ -z "$left" ] || echo "NOTE: terraform.tfvars also sets: $left. GitHub's plan doesn't; add them to ci.tfvars or a TF_VAR_* variable."
ci_model=$(sed -n -E 's/^[[:space:]]*verdict_model[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/p' "$TF/ci.tfvars")
[ "$(raw verdict_model)" = "$ci_model" ] || echo "NOTE: verdict_model in terraform.tfvars differs from ci.tfvars; GitHub's plan would change it."

say "8. first importers run (skip it if cutover 2 already started one)"
ask "run importers.yml on main (importer=all; a manual run never fetches Royal)?"
gh workflow run importers.yml -R "$GH" --ref main -f importer=all
echo "started: https://github.com/$GH/actions/workflows/importers.yml"

say "DONE. Next: file the SiteGround ticket with the WooCommerce/generic IP above. From now on, Terraform changes are PRs:"
echo "      merge, then approve the terraform-apply run as jtouleyrou (docs/app/RUNNING.md, \"Changing the infrastructure\")."
