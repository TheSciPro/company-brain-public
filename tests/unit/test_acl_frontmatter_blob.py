from datetime import date
from pathlib import Path

from nwb.blob.local import LocalBlobStore
from nwb.ingest.acl import needs_grants, parse_acl
from nwb.ingest.frontmatter import split_frontmatter


def test_parse_acl_ids_and_time_window():
    specs = parse_acl(
        ["grp-legal", "NW0011", "grp-auditor-ext (privacy/security clauses, 2026-07-28..10-30)", "junk"],
        groups={"grp-legal", "grp-auditor-ext"},
        people={"NW0011"},
    )
    assert [(s.subject_type, s.subject_id) for s in specs] == [
        ("group", "grp-legal"),
        ("person", "NW0011"),
        ("group", "grp-auditor-ext"),
    ]
    assert (specs[2].valid_from, specs[2].valid_to) == (date(2026, 7, 28), date(2026, 10, 30))


def test_grants_only_for_restricted_tags():
    assert not needs_grants("PUBLIC-INT")
    assert not needs_grants("PERSONAL")
    assert needs_grants("CLIENT-CONF-BRM")
    assert needs_grants(None)  # unknown -> fail closed: only explicit acl can read


def test_frontmatter_lists_quotes_and_nulls():
    meta, body = split_frontmatter(
        '---\ntitle: "A: B"\nacl: [grp-a, NW0001 (x, y), "q,r"]\neffective_from: null\n---\n# Body\n'
    )
    assert meta == {"title": "A: B", "acl": ["grp-a", "NW0001 (x, y)", "q,r"], "effective_from": None}
    assert body == "# Body\n"


def test_blob_store_is_content_addressed(tmp_path: Path):
    store = LocalBlobStore(tmp_path)
    a, b = store.put(b"hello"), store.put(b"hello")
    assert a == b and store.get(a) == b"hello"
    assert store.put(b"other") != a
