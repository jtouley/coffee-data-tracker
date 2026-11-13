#!/usr/bin/env bash
# Print the ingest image for a commit, by digest, and only if GitHub built and signed it from that commit:
#   <repository>/ingest@sha256:<digest>
# Anything else prints why on stderr, prints nothing on stdout and exits non-zero, so a caller that pins what this
# prints can't pin an image that wasn't checked (docs/ingestion/RUNBOOK.md §1 and §4).
#
#   docs/ingestion/verified-ingest-image.sh <repository>/ingest [commit]     # commit: default HEAD
#
# It needs gcloud (read access to the registry, and `gcloud auth configure-docker <region>-docker.pkg.dev` done, so
# gh can read the image) and gh, signed in. It changes nothing.
set -euo pipefail

die() { echo "verified-ingest-image: $*. STOP: pin nothing." >&2; exit 1; }

image="${1:-}"
[[ "$image" =~ ^[a-z0-9.-]+/[a-z0-9._/-]+$ ]] || die "usage: $0 <repository>/ingest [commit] (no tag, no digest)"
sha="${2:-$(git rev-parse HEAD)}" || die "can't read the commit"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "not a full commit sha: '$sha'"
repo="${GITHUB_REPOSITORY:-jtouley/coffee-data-tracker}"

# The workflow tags each image with the commit it was built from. No image: that commit wasn't built (yet).
digest=$(gcloud artifacts docker images describe "$image:$sha" --format='value(image_summary.digest)') ||
  die "no ingest image for $sha (deploy.yml hasn't built it: RUNBOOK §1)"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "no digest for $image:$sha (got '$digest')"

# Signed by deploy.yml on main, from this commit. A forced manual deploy signs too: this proves who built the image
# and from what, not that ci passed on the commit.
gh attestation verify "oci://$image@$digest" --repo "$repo" \
  --signer-workflow "$repo/.github/workflows/deploy.yml" \
  --source-ref refs/heads/main --source-digest "$sha" >&2 ||
  die "no valid provenance for $image@$digest"

echo "$image@$digest"
