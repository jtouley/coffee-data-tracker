# The settings that used to live only in the owner's (gitignored) terraform.tfvars, checked in so that the plan in
# GitHub (.github/workflows/terraform-plan.yml) and the owner's shell plan the same thing. Nothing here is secret:
# the repository is public, and every value below is already visible in this repository, in a job's
# configuration, or harmless. The two private values are not here: billing_account and owner_email come from the
# repository secrets TF_VAR_BILLING_ACCOUNT and TF_VAR_OWNER_EMAIL in CI, and from terraform.tfvars in the shell.
#
# Terraform loads *.auto.tfvars after terraform.tfvars, so a value here wins over the same name there; `-var` on
# the command line still wins over both.
#
# FILL_IN: the five values marked FILL_IN are the owner's to copy from today's terraform.tfvars (or today's
# infrastructure), once, in a PR. Each placeholder is a string of the wrong type or shape, so until it is filled
# in, every plan stops at that variable instead of planning a change. When they are filled in, a shell plan with
# terraform.tfvars holding only billing_account and owner_email must say "No changes" (the parity check). Anything
# else terraform.tfvars sets today moves here too.

# Claude's coffee-or-not verdicts for the build job; the same value as the VERDICT_MODEL repository variable.
verdict_model = "claude-haiku-5-5"

# The ingest image every job runs, by digest: <region>-docker.pkg.dev/<project>/registry-coffee/ingest@sha256:<64
# hex>. Today's pin is the one cutover 2 wrote into terraform.tfvars. A new image is pinned by a one-line PR here
# (docs/ingestion/RUNBOOK.md section 1 reads the digest for a commit and checks its provenance).
ingest_image = "FILL_IN: the pinned image from terraform.tfvars, <repo>/ingest@sha256:<digest>"

# Whether the weekly Cloud Scheduler entries exist: true or false, as they are today.
ingest_schedule_enabled = "FILL_IN: true or false, as in terraform.tfvars today"

# Email recovery on the app (variables.tf): true or false, as today (the default is false).
email = "FILL_IN: true or false, as in terraform.tfvars today (default false)"

# The monthly budget in US dollars, as today (the default is 20).
budget_usd = "FILL_IN: a number, as in terraform.tfvars today (default 20)"

# Per-job memory overrides, as today, e.g. { "crawl-woocommerce" = "4Gi" } (the default is {}).
ingest_job_memory = "FILL_IN: a map, as in terraform.tfvars today (default {})"
