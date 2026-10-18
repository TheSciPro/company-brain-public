"""Parsers for Layer-1 source files (data/directory, data/hris). Pure: files in, typed records out."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from nwb.errors import DataLoadError
from nwb.util.geo import normalize_country


@dataclass(frozen=True, slots=True)
class ScimUser:
    id: str
    name: str
    email: str | None
    title: str | None
    department: str | None
    division: str | None
    grade: str | None
    location: str | None
    hire_date: date | None
    external: bool
    active: bool
    country: str | None = None


@dataclass(frozen=True, slots=True)
class ScimGroup:
    id: str
    name: str
    member_ids: tuple[str, ...]
    created: date
    window_from: date | None
    window_to: date | None


@dataclass(frozen=True, slots=True)
class Worker:
    id: str
    name: str
    grade: str
    title: str
    unit: str
    location: str
    manager_id: str | None
    hire_date: date


@dataclass(frozen=True, slots=True)
class JobEvent:
    id: str
    worker_id: str
    event_type: str
    effective: date
    before: dict[str, Any]
    after: dict[str, Any]
    source_ref: str


def _d(v: str | None) -> date | None:
    return date.fromisoformat(v[:10]) if v else None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DataLoadError(f"{path}: {exc}") from exc


class DirectorySource:
    """data/directory: SCIM users and groups, ACL classification map."""

    _ENT = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
    _HR = "urn:northwind:params:scim:schemas:extension:hr:1.0:User"

    def __init__(self, root: Path) -> None:
        self._root = root / "directory"

    def users(self) -> list[ScimUser]:
        out: list[ScimUser] = []
        for u in _load_json(self._root / "scim_users.json")["Resources"]:
            ent, hr = u.get(self._ENT, {}), u.get(self._HR, {})
            emails = [e["value"] for e in u.get("emails", []) if e.get("primary")]
            out.append(
                ScimUser(
                    id=u["id"],
                    name=u.get("displayName") or u["name"]["formatted"],
                    email=emails[0] if emails else u.get("userName"),
                    title=u.get("title"),
                    department=ent.get("department"),
                    division=ent.get("division"),
                    grade=hr.get("grade"),
                    location=hr.get("location_code"),
                    hire_date=_d(hr.get("hire_date")),
                    external=u.get("userType") != "Employee",
                    active=bool(u.get("active", True)),
                    country=_country(u.get("addresses")),
                )
            )
        return out

    def groups(self) -> list[ScimGroup]:
        out: list[ScimGroup] = []
        for g in _load_json(self._root / "scim_groups.json")["Resources"]:
            meta = g.get("x_northwind", {})
            window = meta.get("access_window") or {}
            out.append(
                ScimGroup(
                    id=g["id"],
                    name=g["displayName"],
                    member_ids=tuple(m["value"] for m in g.get("members", [])),
                    created=_d(meta.get("created_at")) or date(2025, 1, 1),
                    window_from=_d(window.get("valid_from")),
                    window_to=_d(window.get("valid_to")),
                )
            )
        return out

    def acl_classifications(self) -> dict[str, list[str]]:
        data = _load_json(self._root / "acl_classifications.json")
        return {str(k): [str(s) for s in v] for k, v in data["classifications"].items()}


class HrisSource:
    """data/hris: workers.csv (current state) and job_events.csv (dated history)."""

    def __init__(self, root: Path) -> None:
        self._root = root / "hris"

    def workers(self) -> list[Worker]:
        with (self._root / "workers.csv").open(newline="") as fh:
            return [
                Worker(
                    id=r["worker_id"],
                    name=r["name"],
                    grade=r["grade"],
                    title=r["title"],
                    unit=r["unit"],
                    location=r["location"],
                    manager_id=r["manager_id"] or None,
                    hire_date=date.fromisoformat(r["hire_date"]),
                )
                for r in csv.DictReader(fh)
            ]

    def job_events(self) -> list[JobEvent]:
        def obj(v: str) -> dict[str, Any]:
            if not v or v == "null":
                return {}
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}

        with (self._root / "job_events.csv").open(newline="") as fh:
            events = [
                JobEvent(
                    id=r["id"],
                    worker_id=r["worker_id"],
                    event_type=r["event_type"],
                    effective=date.fromisoformat(r["effective_date"]),
                    before=obj(r["before"]),
                    after=obj(r["after"]),
                    source_ref=r["source_ref"],
                )
                for r in csv.DictReader(fh)
            ]
        return sorted(events, key=lambda e: (e.worker_id, e.effective, e.id))


def _country(addresses: object) -> str | None:
    """ISO country of the primary work address (SCIM ``addresses[].country``), normalised to alpha-2."""
    if not isinstance(addresses, list):
        return None
    ranked = sorted((a for a in addresses if isinstance(a, dict)), key=lambda a: not a.get("primary"))
    for a in ranked:
        if a.get("country"):
            return normalize_country(str(a["country"]))
    return None
