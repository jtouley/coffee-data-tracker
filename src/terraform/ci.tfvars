# Settings for the plan in GitHub (.github/workflows/terraform-plan.yml), which passes this file with
# `-var-file=ci.tfvars`. Not an *.auto.tfvars on purpose: the owner's shell never loads it, so the shell's
# terraform.tfvars keeps working exactly as before. Only real, public values go here, never a placeholder.
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
# tags the digest set here so the registry's cleanup keeps it. Unset: Google's sample image. usage_enabled: true once
# the apply account can act as the collector's account and write log exclusions; usage_token: true once the
# USAGE_HF_TOKEN secret has a value (RUNNING.md, "Usage").
