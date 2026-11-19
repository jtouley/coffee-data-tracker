# Checked-in settings for every plan of this root, and their one source: GitHub's plan and apply
# (terraform-plan.yml, terraform-apply.yml) and the owner's scripts (scripts/tf_break_glass.sh,
# docs/ingestion/final-shell-run.sh) all pass it with `-var-file=ci.tfvars`, after terraform.tfvars, so a value set
# here wins over the same one there. A plan by hand from the shell must pass it too, or it proposes undoing these
# (the usage collector would be destroyed). Not an *.auto.tfvars: the guarded routes load it explicitly, and the
# existing hand commands keep working as written. Only real, public values go here, never a placeholder.
#
# Where the other values come from in CI (docs/ingestion/RUNBOOK.md section 9):
# - repository secrets TF_VAR_BILLING_ACCOUNT and TF_VAR_OWNER_EMAIL: billing_account and owner_email;
# - repository variables TF_VAR_INGEST_IMAGE and TF_VAR_INGEST_SCHEDULE_ENABLED (required), and
#   TF_VAR_EMAIL, TF_VAR_BUDGET_USD and TF_VAR_INGEST_JOB_MEMORY (optional: unset means the default): the values the
#   owner's terraform.tfvars holds today.
# Once those values have moved here and a shell plan with only the two private values in terraform.tfvars says
# "No changes", this file can become ci.auto.tfvars, with the docs that say "terraform.tfvars" updated with it.

# Claude's coffee-or-not verdicts for the build job; the same value as the VERDICT_MODEL repository variable.
verdict_model = "claude-haiku-5-5"

# The usage collector (usage.tf). usage_image: an app image deploy.yml built from main, by digest (the "Build and
# push" step's log and the attest job name it). Bumping it here, in a PR, is how the collector is deployed; deploy.yml
# tags the digest set here so the registry's cleanup keeps it. usage_enabled: true once the apply account can act as
# the collector's account, write log exclusions and read the registry; usage_token: true once the USAGE_HF_TOKEN
# secret has an enabled version (RUNNING.md, "Usage"). usage_enabled needs both usage_image and usage_token
# (usage.tf's precondition).
# Pinned: deploy.yml run 44253151612 on main at 10de76a (#181), "Build and push" step; its attest job signed this
# digest (attestation 68996644).
usage_image   = "us-central1-docker.pkg.dev/coffee-data-tracker/registry-coffee/app@sha256:5945b9b7628093efb159d425584b05c0b8e4a6776a1b83ee4fe2f8d967d1c537"
usage_enabled = true
usage_token   = true
