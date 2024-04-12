// IAM policy for unauthenticated access - needed to be added due to CORS issue
resource "google_cloudfunctions_function_iam_member" "public_invoker" {
  cloud_function = google_cloudfunctions_function.coffee_data_function.name

  role   = "roles/cloudfunctions.invoker"
  member = "allUsers"
}