JSON schemas from the Bitol project, copied unchanged so the contract tests run offline. Apache-2.0,
Copyright The Bitol Contributors.

- `odcs-json-schema-v3.2.0.json`: https://github.com/bitol-io/open-data-contract-standard, path
  `schema/odcs-json-schema-v3.2.0.json`. Checked on 2026-11-01 against `main` at commit
  d3e1cb3e69849e05c9a7522abed9b27fb9af50d7: byte-identical, sha256
  `edb41f33ec46e84780e99872ab2bd67f074959d2bf3e9c9fc54e61f8982b0d93` (first vendored from commit 04d1bfd,
  2026-10-25). Upstream `dev` carries a newer, rolling v3.2.0 (at 448f7b9 the quality operators' `oneOf` becomes
  `anyOf`). Never re-vendor from that rolling `dev` file: re-vendor from an upstream release, or from the dated
  snapshot ODCS publishes (`odcs-json-schema-v3.2.0-YYYYMMDD.json`), pinned to its commit, and record that commit
  and the sha256 here.
- `odps-json-schema-v1.1.0.json`: https://github.com/bitol-io/open-data-product-standard (commit 5f54a17, 2026-09-11)
