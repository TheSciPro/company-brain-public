"""Who is calling. A port so the demo header can be swapped for real auth (OIDC/JWT) without touching routes."""

from __future__ import annotations

from typing import Protocol

from fastapi import Request

from nwb.db.repo_directory import DirectoryReader
from nwb.errors import AccessDeniedError
from nwb.ports import SqlDatabase


class IdentityProvider(Protocol):
    def person_id(self, request: Request) -> str: ...


class DemoHeaderIdentity:
    """Demo only: trusts ``X-NWB-Person`` (the UI persona switcher) after checking the person exists.

    Production replaces this with a verified token; nothing downstream reads identity from request bodies.
    """

    HEADER = "X-NWB-Person"

    def __init__(self, db: SqlDatabase) -> None:
        self._db = db

    def person_id(self, request: Request) -> str:
        pid = request.headers.get(self.HEADER, "").strip()
        if not pid:
            raise AccessDeniedError(f"missing {self.HEADER} header")
        with self._db.unit_of_work() as uow:
            if DirectoryReader(uow).person(pid) is None:
                raise AccessDeniedError("unknown person")
        return pid
