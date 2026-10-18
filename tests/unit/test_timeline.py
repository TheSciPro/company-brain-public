from datetime import date

import pytest
from pydantic import ValidationError

from nwb.db.models import Membership
from nwb.ingest.acl import classify_tag
from nwb.ingest.sources import JobEvent
from nwb.ingest.timeline import build_memberships, build_reporting_lines


def ev(i, wid, kind, d, before=None, after=None):
    return JobEvent(i, wid, kind, date.fromisoformat(d), before or {}, after or {}, "test")


EVENTS = [
    ev("1", "P1", "Hire", "2020-01-01", after={"unit": "RTL"}),
    ev("2", "P1", "Promotion", "2022-01-01", {"grade": "G3"}, {"grade": "G4"}),
    ev("3", "P1", "Reorganization", "2026-05-01", {"unit": "RTL", "manager": "M1"}, {"unit": "CON", "manager": "M2"}),
    ev("4", "P2", "Hire", "2021-03-01", after={"unit": "BFS"}),
    ev("5", "P2", "Termination", "2025-06-30", {"status": "active"}, {"status": "terminated"}),
]


def test_unit_change_closes_previous_membership_day_before():
    ms = sorted(build_memberships(EVENTS, {"P1": True}), key=lambda m: (m.person_id, m.valid_from))
    p1 = [m for m in ms if m.person_id == "P1"]
    assert [(m.unit_id, m.valid_from, m.valid_to, m.is_leader) for m in p1] == [
        ("RTL", date(2020, 1, 1), date(2026, 4, 30), True),
        ("CON", date(2026, 5, 1), None, True),
    ]


def test_termination_closes_membership_on_last_day():
    (p2,) = [m for m in build_memberships(EVENTS) if m.person_id == "P2"]
    assert p2.valid_to == date(2025, 6, 30)
    assert p2.active_on(date(2025, 6, 30)) and not p2.active_on(date(2025, 7, 1))


def test_reporting_lines_walk_back_from_current_manager():
    lines = build_reporting_lines(EVENTS, {"P1": "M2"}, {"P1": date(2020, 1, 1)})
    got = sorted((ln.manager_id, ln.valid_from, ln.valid_to) for ln in lines)
    assert got == [("M1", date(2020, 1, 1), date(2026, 4, 30)), ("M2", date(2026, 5, 1), None)]


def test_dated_rows_reject_inverted_ranges():
    with pytest.raises(ValidationError):
        Membership(person_id="x", unit_id="u", valid_from=date(2026, 2, 1), valid_to=date(2026, 1, 1))


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("PUBLIC-INT", ("internal", None)),
        ("MGR", ("confidential", "leadership")),
        ("RESTRICTED-FIN", ("highly_confidential", None)),
        ("SOMETHING-NEW", ("highly_confidential", None)),  # fail closed
    ],
)
def test_tag_classification_fails_closed(tag, expected):
    assert classify_tag(tag) == expected
