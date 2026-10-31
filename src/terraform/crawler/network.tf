# Its own network, not `default`: a project's default network usually carries default-allow-ssh and
# default-allow-rdp from 0.0.0.0/0. Here the only ingress is SSH from Google's IAP range; egress is open (the crawl).

resource "google_compute_network" "crawler" {
  name                    = "crawler"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "crawler" {
  name          = "crawler"
  network       = google_compute_network.crawler.id
  region        = var.region
  ip_cidr_range = "10.80.0.0/28"
}

# IAP TCP forwarding's documented source range (cloud.google.com/iap/docs/using-tcp-forwarding). Nothing else gets in:
# a VPC's implied rules deny all other ingress.
resource "google_compute_firewall" "iap_ssh" {
  name          = "crawler-allow-iap-ssh"
  network       = google_compute_network.crawler.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["crawler"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# The fixed egress address. Premium tier, so it stays the address the stores see.
resource "google_compute_address" "egress" {
  name         = "crawler-egress"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
  depends_on   = [google_project_service.apis]
}
