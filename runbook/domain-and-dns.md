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
| CAA | `@` | `0 issue "letsencrypt.org"` | only Let's Encrypt (which Pages uses) may issue certificates |
| TXT | `@` | `v=spf1 -all` | the domain sends no email; receivers should reject mail claiming to be from it |
| TXT | `_dmarc` | `v=DMARC1; p=reject; sp=reject; adkim=s; aspf=s` | same, enforced with DMARC |
| CNAME | `app` | **TBD** (HF Space or container host) | the shot-logging app at `app.registry.coffee`; add it once the app's host is chosen |
| TXT | `_github-pages-challenge-jtouley` | the code GitHub shows (see [github-pages.md](github-pages.md)) | proves the domain is yours; keep it |

TTL: Automatic (Namecheap shows 30 min for some; either is fine).

## Setting them up

1. **Nameservers.** Domain List → **Manage** (registry.coffee) → **Domain** tab → **Nameservers** → choose
   **Namecheap BasicDNS** → click the green ✓. They should read `dns1.registrar-servers.com` and
   `dns2.registrar-servers.com`.
2. **Host records.** Open the **Advanced DNS** tab. Delete the parking records Namecheap adds to a new domain
   (a `CNAME www → parkingpage.namecheap.com` and a `URL Redirect @`), then **Add New Record** for each row above.
3. Wait 5–30 minutes and run `runbook/check-dns.sh`.

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
