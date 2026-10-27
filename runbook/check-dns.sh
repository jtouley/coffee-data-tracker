#!/usr/bin/env bash
# Print the DNS records registry.coffee depends on, as Google (8.8.8.8) and Cloudflare (1.1.1.1) see them.
# Uses DNS-over-HTTPS, so it only needs curl and python3. Usage: runbook/check-dns.sh [domain]
set -euo pipefail
domain="${1:-registry.coffee}"
failed=0
ask() {  # resolver url, name, type
  curl -fsS -H 'accept: application/dns-json' "$1?name=$2&type=$3" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ans = [a["data"] for a in d.get("Answer", []) if a.get("type") in (1, 5, 16, 257)]
st = d.get("Status")
print(", ".join(sorted(ans)) if ans else {0: "(none)", 3: "NXDOMAIN"}.get(st, "status " + str(st)))'
}
check() {  # name, type, expected substring
  local g c mark
  g=$(ask https://dns.google/resolve "$1" "$2")
  c=$(ask https://cloudflare-dns.com/dns-query "$1" "$2")
  if [[ "$g" == *"$3"* && "$c" == *"$3"* && "$g$c" != *NXDOMAIN* && "$g$c" != *"(none)"* ]]; then mark="ok  "
  else mark="FAIL"; failed=1; fi
  printf '%s %-6s %-46s google: %s\n%58s cloudflare: %s\n' "$mark" "$2" "$1" "$g" "" "$c"
}
check "$domain" A 185.199.108.153
check "www.$domain" CNAME jtouley.github.io
check "$domain" CAA letsencrypt.org
check "$domain" TXT "v=spf1 -all"
check "_dmarc.$domain" TXT "p=reject"
check "_github-pages-challenge-jtouley.$domain" TXT ""
exit $failed
