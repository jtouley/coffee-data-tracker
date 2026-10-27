# Domain and DNS (Namecheap)

registry.coffee is registered at Namecheap (order 217877177, 2026-10-27, one year, with Namecheap's free domain privacy). DNS is
hosted by Namecheap **BasicDNS**, which is free; nothing else needs to be bought.

## Records

These are the records the site needs. Check them any time with `runbook/check-dns.sh`.

| Type | Host | Value | Why |
|---|---|---|---|
| A | `@` | `185.199.108.153` | GitHub Pages |
| A | `@` | `185.199.109.153` | GitHub Pages |
| A | `@` | `185.199.110.153` | GitHub Pages |
| A | `@` | `185.199.111.153` | GitHub Pages |
| CNAME | `www` | `jtouley.github.io.` | `www.registry.coffee` → Pages, which redirects it to the apex |
| CAA | `@` | `0 issue "letsencrypt.org"` | Let's Encrypt may issue certificates (GitHub Pages uses it) |
| CAA | `@` | `0 issue "pki.goog"` | Google Trust Services may issue certificates (Cloud Run's managed certificates can come from either) |
| TXT | `@` | `v=spf1 -all` | the domain sends no email; receivers should reject mail claiming to be from it |
| TXT | `_dmarc` | `v=DMARC1; p=reject; sp=reject; adkim=s; aspf=s` | same, enforced with DMARC |
| CNAME | `app` | `ghs.googlehosted.com.` | the shot-logging app at `app.registry.coffee`, on Cloud Run (see below) |
| TXT | `@` | `google-site-verification=…` (the code Search Console shows) | proves the domain to Google so Cloud Run can map it; keep it |
| TXT | `_github-pages-challenge-jtouley` | the code GitHub shows (see [github-pages.md](github-pages.md)) | proves the domain is yours; keep it |

TTL: Automatic (Namecheap shows 30 min for some; either is fine).

## Setting them up

1. **Nameservers.** Domain List → **Manage** (registry.coffee) → **Domain** tab → **Nameservers** → choose
   **Namecheap BasicDNS** → click the green ✓. They should read `dns1.registrar-servers.com` and
   `dns2.registrar-servers.com`.
2. **Host records.** Open the **Advanced DNS** tab. Delete the parking records Namecheap adds to a new domain
   (a `CNAME www → parkingpage.namecheap.com` and a `URL Redirect @`), then **Add New Record** for each row above.
3. Wait 5–30 minutes and run `runbook/check-dns.sh`.

## app.registry.coffee on Cloud Run

The app runs on Google Cloud Run in `us-central1`, with a Cloud Run **domain mapping** for `app.registry.coffee`.

1. **Verify the domain with Google.** Cloud Run only maps domains that the deploying Google account has verified in
   [Google Search Console](https://search.google.com/search-console). Add a *Domain* property for `registry.coffee`
   (not a URL prefix), copy the `google-site-verification=…` value, and add it as a TXT record on host `@` in
   Namecheap → Advanced DNS. It sits next to the SPF TXT record; both are fine on `@`. Click **Verify** in Search
   Console. `gcloud domains list-user-verified` should then list it. If the Cloud Run deploy uses a service
   account, add it as an owner of the property too.
2. **Map the domain.** Cloud Run → the service → **Manage custom domains** → **Add mapping** → `app.registry.coffee`
   (or `gcloud beta run domain-mappings create --service <service> --domain app.registry.coffee --region us-central1`).
   Google answers with the record to add, which is a CNAME `app` → `ghs.googlehosted.com.`
3. **Add the CNAME** in Namecheap: host `app` (just that), value `ghs.googlehosted.com.`
4. **Wait for the certificate.** Google issues it automatically once the CNAME resolves, usually within 15 minutes,
   though it can take up to a day. The CAA records must allow `pki.goog` as well as `letsencrypt.org`, or issuance
   fails.

Domain mappings are only available in some regions, and `us-central1` is one of them. Google still lists the feature
as preview; if it's ever withdrawn, the fallback is a global external Application Load Balancer, which costs about
$18 a month. Cloud Run itself bills per request and has a monthly free tier; set a budget alert in Billing.

Mapping `app` doesn't touch the apex: registry.coffee and www stay on GitHub Pages.

## Gotchas

- **No Advanced DNS tab.** It's free, but hidden while the nameservers are set to *Custom DNS*. Switch to
  BasicDNS (step 1) and reload.
- **Custom DNS with empty fields.** The "DNS / Host Records" wizard defaults to *Custom DNS*. Saving it with blank
  nameserver fields takes the domain offline. Pick *Namecheap BasicDNS*.
- **Namecheap appends the domain to the Host.** Enter `_dmarc`, not `_dmarc.registry.coffee`. Typing the full name
  creates `_dmarc.registry.coffee.registry.coffee`. (This happened with the GitHub challenge record the first time.)
- **The bulk wizard has no CAA type.** Domain List → select domains → "DNS / Host Records" only offers A, AAAA,
  CNAME, NS, SPF, TXT and URL Redirect. Add CAA from the domain's own Advanced DNS tab. Don't type a CAA value into a
  TXT record; it does nothing.
- **Re-running the bulk wizard can overwrite records.** On its nameserver step, *I want to update Host Records*
  replaces what's there. To change one record, use Advanced DNS instead, or tick *leave Host Records and Mail
  Settings*.
- **Resolvers cache "not found".** A record you just fixed can show on Cloudflare (1.1.1.1) before Google (8.8.8.8).
  `check-dns.sh` asks both.

## Account and renewal

- Two-factor authentication on at Namecheap (Profile → Security).
- **Registrar Lock** on (Domain tab), **Auto-Renew** on, and a card on file that won't expire.
- Renewal: the first year was $6.98; .coffee renews at roughly $48/year at Namecheap (third-party price tracker,
  July 2026). Some registrars renew .coffee for about half that. To move: unlock the domain, get the EPP/auth code
  from the Domain tab, start the transfer at the new registrar (it adds a year), then recreate the records above
  there **before** switching nameservers. A transfer isn't possible in the first 60 days after registration.
