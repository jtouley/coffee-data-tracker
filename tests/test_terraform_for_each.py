"""A for_each set or list must be known at plan: no element may come from a resource's attribute.

Terraform refuses a for_each whose keys depend on values known only after apply, so a first apply stops at plan
(the bootstrap's custom-role ids did this). Those values go in a map's values, under static keys, instead.
"""

import re
from pathlib import Path

ROOTS = [
    Path(__file__).resolve().parents[1] / "src/terraform",
    Path(__file__).resolve().parents[1] / "src/terraform/bootstrap",
]
SET = re.compile(r"^\s*for_each\s*=\s*(?:toset\()?\[(.*?)\]", re.M | re.S)
RESOURCE_REF = re.compile(r"(?<![\w.\"])(?:google|data\.google)_[a-z0-9_]+\.[a-z0-9_]+")


def test_no_for_each_set_or_list_holds_a_resource_attribute():
    checked = 0
    for root in ROOTS:
        for tf in sorted(root.glob("*.tf")):
            for m in SET.finditer(tf.read_text()):
                body = "\n".join(line.split("#")[0] for line in m.group(1).splitlines())
                assert not RESOURCE_REF.search(body), (
                    f"{tf.name}: for_each element from a resource: {m.group(0)[:80]}"
                )
                checked += 1
    assert checked >= 3  # the APIs, the ingest APIs and the bootstrap's role lists, at least


def test_the_check_catches_the_bootstrap_bug():
    bad = 'for_each = toset([\n    "roles/browser", # x\n    google_project_iam_custom_role.tf_secrets.id,\n  ])'
    assert any(RESOURCE_REF.search(m.group(1)) for m in SET.finditer(bad))
