# admin.registry.coffee (docs/design/admin-registry.md §5.3): the account its service runs as, alone, ahead of the
# rest of the admin (#125). The bootstrap root's grants on it (the apply account acting as it, its Cloud SQL project
# grants) need it to exist first, so it is applied before them and before the service that runs as it. It holds no
# role here; the admin's own changes add its grants.

resource "google_service_account" "admin" {
  account_id   = "${var.service}-admin-run"
  display_name = "admin.registry.coffee (runtime)"
}
