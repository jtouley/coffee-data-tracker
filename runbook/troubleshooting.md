# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| registry.coffee shows a Namecheap parking page | parking records still there, or DNS not propagated | delete them in Advanced DNS; run `runbook/check-dns.sh` |
| registry.coffee shows GitHub's 404 | no deploy from `main` yet, or the custom domain isn't set in the repo's Pages settings | [first-run.md](first-run.md), [github-pages.md](github-pages.md) |
| Pages: "DNS check unsuccessful" | A records wrong, or GitHub cached an old answer | check with `check-dns.sh`, wait 10 min, click "Check again" |
| Pages: "Enforce HTTPS" greyed out | certificate not issued yet | wait up to an hour; if it stays greyed out, remove and re-add the custom domain |
| HTTPS certificate fails to issue | the CAA record doesn't allow `letsencrypt.org` | fix the CAA record |
| Domain verification fails | challenge TXT host typed as the full name | the host must be `_github-pages-challenge-jtouley` only |
| `weekly` fails at "Check lake config" | `LAKE_URL` not set | set the repository variable |
| `weekly` fails uploading to the lake | `HF_TOKEN` missing, expired or without write access to the dataset | new fine-grained token, update the secret |
| `deploy` job fails: "branch is not allowed to deploy" | run from a branch other than `main` | run it from `main` |
| Launch page "Get the dataset" goes to GitHub | `PUBLIC_EXPORT_URL` isn't an `hf://datasets/...` URL | set it; the next run picks it up |
| The catalog app says "Couldn't load the catalog" | `app/catalog.json` missing from the deploy | check the `run` job's site step |
