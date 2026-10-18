"""Ports (hexagonal architecture): small Protocols the domain depends on. Adapters implement them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

Row = dict[str, Any]
Params = Mapping[str, Any]


class Dialect(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class UnitOfWork(Protocol):
    """One transaction. Committed on clean exit of the context manager, rolled back on error."""

    dialect: Dialect

    def execute(self, sql: str, params: Params | None = None) -> None: ...
    def executemany(self, sql: str, rows: Sequence[Params]) -> None: ...
    def fetchall(self, sql: str, params: Params | None = None) -> list[Row]: ...
    def fetchone(self, sql: str, params: Params | None = None) -> Row | None: ...
    def executescript(self, script: str) -> None: ...


class SqlDatabase(Protocol):
    dialect: Dialect

    def unit_of_work(
        self, *, as_person: str | None = None, asof: date | None = None, as_company: str | None = None
    ) -> AbstractContextManager[UnitOfWork]: ...

    def close(self) -> None: ...


class GraphStore(Protocol):
    def query(self, cypher: str, params: Params | None = None) -> list[list[Any]]: ...
    def ro_query(self, cypher: str, params: Params | None = None) -> list[list[Any]]: ...


class AccessPolicy(Protocol):
    """The single access decision (AGENTS §5). Identity is passed by the runtime, never by the model."""

    def visible_record_ids(self, person_id: str, asof: date) -> list[str]: ...
    def can_read_record(self, person_id: str, asof: date, record_id: str) -> bool: ...
    def visible_document_ids(self, person_id: str, asof: date) -> list[str]: ...


class BlobStore(Protocol):
    def put(self, data: bytes) -> str: ...
    def get(self, url: str) -> bytes: ...
