cat > /tmp/diag.py <<'PY'
"""SiteGround 202 diagnostic: one environment, the crawler's own httpx client, no writes.

Prints (1) the TLS stack, (2) the egress IP, (3) the ClientHello as two fingerprint echo services saw it, and
(4) one robots.txt per SiteGround roaster, with the status and the TLS/HTTP details negotiated for it.
Sends 2 + 2 + len(HOSTS) GET requests in total, one at a time. Reads no environment variable values.
"""

import json
import os
import ssl
import sys
import time

import certifi
import httpcore
import httpx

from coffeedata.http import USER_AGENT, PoliteClient

HOSTS = os.environ.get("DIAG_HOSTS", "canopycoffeeroasters.com joevangogh.com").split()
LABEL = os.environ.get("DIAG_LABEL", "unlabeled")

client = PoliteClient()._client  # the exact httpx.Client the crawler sends with (headers, timeouts, limits)


def tls_info(r: httpx.Response) -> dict:
    out = {"http_version": r.http_version}
    stream = r.extensions.get("network_stream")
    if stream is not None:
        so = stream.get_extra_info("ssl_object")
        peer = stream.get_extra_info("server_addr")
        out["peer"] = peer[0] if peer else None
        if so is not None:
            out.update(tls=so.version(), cipher=so.cipher()[0], alpn=so.selected_alpn_protocol())
    return out


def get(url: str) -> httpx.Response | None:
    t = time.monotonic()
    try:
        r = client.get(url)
        r.read()
        r.elapsed_s = round(time.monotonic() - t, 2)  # type: ignore[attr-defined]
        return r
    except httpx.HTTPError as e:
        print(f"  {url}: {type(e).__name__}: {e}")
        return None


print(f"== {LABEL} | {sys.executable}")
print(f"python {sys.version.split()[0]} | {ssl.OPENSSL_VERSION} | httpx {httpx.__version__} "
      f"| httpcore {httpcore.__version__} | certifi {certifi.__version__}")
print(f"OPENSSL_CONF set: {'OPENSSL_CONF' in os.environ} | client headers: {dict(client.headers)}")

r = get("https://api.ipify.org")
print(f"egress ip: {r.text.strip() if r is not None and r.status_code == 200 else '?'}")

r = get("https://tls.peet.ws/api/all")
if r is not None and r.status_code == 200:
    j = r.json()
    t = j.get("tls", {})
    groups, shares = None, None
    for ext in t.get("extensions", []):
        groups = ext.get("supported_groups", groups)
        shares = [s.get("group") for s in ext.get("shared_keys", [])] or shares
    print(f"tls.peet.ws: ip={j.get('ip')} http={j.get('http_version')} ja3_hash={t.get('ja3_hash')} "
          f"ja4={t.get('ja4')} peetprint_hash={t.get('peetprint_hash')} ciphers={len(t.get('ciphers', []))}")
    print(f"  supported_groups={groups}")
    print(f"  key_shares={shares}")
else:
    print(f"tls.peet.ws: {getattr(r, 'status_code', 'no response')}")

r = get("https://tls.browserleaks.com/json")
if r is not None and r.status_code == 200:
    j = r.json()
    print(f"browserleaks: ja3_hash={j.get('ja3_hash')} ja4={j.get('ja4')} ja3n_hash={j.get('ja3n_hash')}")
else:
    print(f"browserleaks: {getattr(r, 'status_code', 'no response')}")

for host in HOSTS:
    r = get(f"https://{host}/robots.txt")
    if r is None:
        continue
    h = r.headers
    print(json.dumps({"host": host, "status": r.status_code, "elapsed_s": r.elapsed_s, **tls_info(r),
                      "sg_captcha": h.get("sg-captcha"), "server": h.get("server"),
                      "host_header": h.get("host-header"), "content_type": h.get("content-type")}))
    time.sleep(1.0)  # the crawler's per-host spacing; hosts differ anyway
PY
printf 'openssl_conf = openssl_init\n[openssl_init]\nssl_conf = ssl_sect\n[ssl_sect]\nsystem_default = sd\n[sd]\nGroups = x25519:secp256r1:x448:secp521r1:secp384r1:ffdhe2048:ffdhe3072:ffdhe4096:ffdhe6144:ffdhe8192\n' > /tmp/legacy.cnf
L="${DIAG_LABEL:-run}"
if [ "${DIAG_LEGACY:-1}" = 1 ]; then OPENSSL_CONF=/tmp/legacy.cnf DIAG_LABEL="$L / legacy groups (OpenSSL 3.0 defaults)" python /tmp/diag.py; sleep 2; fi
DIAG_LABEL="$L / image default TLS" python /tmp/diag.py
