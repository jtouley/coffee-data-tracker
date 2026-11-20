"""Every service account the bootstrap root grants on by name (`locals.managed_account` in
src/terraform/bootstrap/iam.tf) is one the main root creates: a grant on an account that doesn't exist yet fails the
bootstrap apply (the owner's, by hand, and step 6 of docs/ingestion/final-shell-run.sh, which applies the main root
first). So an account is added to the main root, and applied, before the bootstrap names it."""

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[1] / "src/terraform"


def _main_root_accounts() -> set[str]:
    code = "".join(tf.read_text() for tf in sorted(TF.glob("*.tf")))
    blocks = re.findall(r'^resource "google_service_account" "\w+" \{\n(.*?)^\}', code, re.M | re.S)
    ids = set()
    for b in blocks:
        m = re.search(r'account_id\s*=\s*(?:"([^"]+)"|local\.(\w+))', b)
        assert m, b
        if m.group(2):  # a local, such as usage.tf's usage_service
            local = re.search(rf'^\s*{m.group(2)}\s*=\s*"([^"]+)"', code, re.M)
            assert local, m.group(2)
        ids.add(m.group(1) or local.group(1))
    return ids


def _bootstrap_accounts() -> set[str]:
    code = (TF / "bootstrap" / "iam.tf").read_text()
    block = re.search(r"managed_account = \{\s*for k, id in \{(.*?)\n\s*\} : k =>", code, re.S)
    assert block, "managed_account not found in the bootstrap"
    return set(re.findall(r'=\s*"([^"]+)"', block.group(1)))


def test_the_bootstrap_names_only_accounts_the_main_root_creates():
    named = _bootstrap_accounts()
    assert named, "no accounts parsed"
    assert named <= _main_root_accounts(), sorted(named - _main_root_accounts())


def test_the_admins_account_is_in_the_main_root():
    code = (TF / "admin.tf").read_text()
    assert re.search(
        r'resource "google_service_account" "admin" \{\n'
        r'  account_id   = "\$\{var\.service\}-admin-run"\n'
        r'  display_name = "admin\.registry\.coffee \(runtime\)"\n\}',
        code,
    )
