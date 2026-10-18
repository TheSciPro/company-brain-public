"""Clients as entities, built from the data: contract/document frontmatter names the client and its parties.

A party name that appears for more than one client (or contains the company's own name) is the company itself, not
a client. Aliases are the client's party names, its id and the name tokens unique to it, so "the Acme account"
resolves without any client name in code or config.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from nwb.ports import SqlDatabase, UnitOfWork

_TOKEN = re.compile(r"[A-Za-z0-9]{3,}")


@dataclass(frozen=True, slots=True)
class Client:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()


@dataclass
class ClientCatalogBuilder:
    """Collects (client id, party names, source id) while documents load; ``build()`` decides what is a client."""

    company_name: str
    _parties: dict[str, set[str]] = field(default_factory=dict)
    _sources: dict[str, set[str]] = field(default_factory=dict)

    def add(self, client_id: str, parties: Iterable[str], source_id: str) -> None:
        self._parties.setdefault(client_id, set()).update(p.strip() for p in parties if p and p.strip())
        self._sources.setdefault(client_id, set()).add(source_id)

    def build(self) -> list[Client]:
        own = {t.lower() for t in _TOKEN.findall(self.company_name)}
        seen_in: dict[str, set[str]] = {}
        for cid, parties in self._parties.items():
            for p in parties:
                seen_in.setdefault(p, set()).add(cid)
        token_owners: dict[str, set[str]] = {}
        out: list[Client] = []
        named: dict[str, list[str]] = {}
        for cid, parties in self._parties.items():
            mine = sorted(
                p for p in parties if len(seen_in[p]) == 1 and not own & {t.lower() for t in _TOKEN.findall(p)}
            )
            named[cid] = mine
            for p in mine:
                for t in _TOKEN.findall(p):
                    token_owners.setdefault(t.lower(), set()).add(cid)
        for cid in sorted(self._parties):
            mine = named[cid]
            if not mine:  # an account no document names an outside party for is internal, not a client
                continue
            tokens = sorted({t for p in mine for t in _TOKEN.findall(p) if token_owners[t.lower()] == {cid}})
            aliases = tuple(dict.fromkeys([cid, *mine, *tokens]))
            out.append(Client(cid, mine[0] if mine else cid, aliases, tuple(sorted(self._sources[cid]))))
        return out


class ClientRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def replace_all(self, company_id: str, clients: list[Client]) -> int:
        self._uow.execute("DELETE FROM clients WHERE company_id = :c", {"c": company_id})
        self._uow.executemany(
            "INSERT INTO clients (id, company_id, name, aliases, source_ids) VALUES (:id, :c, :n, :a, :s)",
            [
                {"id": c.id, "c": company_id, "n": c.name, "a": json.dumps(c.aliases), "s": json.dumps(c.source_ids)}
                for c in clients
            ],
        )
        return len(clients)

    def all(self) -> list[Client]:
        rows = self._uow.fetchall("SELECT id, name, aliases, source_ids FROM clients ORDER BY id")
        return [
            Client(str(r["id"]), str(r["name"]), tuple(json.loads(r["aliases"])), tuple(json.loads(r["source_ids"])))
            for r in rows
        ]


class ClientDirectory:
    """Resolve what the user typed (a short name, the full legal name, the id) to one client, or None."""

    def __init__(self, db: SqlDatabase) -> None:
        self._db = db

    def all(self) -> list[Client]:
        with self._db.unit_of_work() as uow:
            return ClientRepository(uow).all()

    def get(self, client_id: str) -> Client | None:
        return next((c for c in self.all() if c.id == client_id), None)

    def resolve(self, text: str | None) -> Client | None:
        if not text or not text.strip():
            return None
        want = {t.lower() for t in _TOKEN.findall(text)}
        exact = text.strip().lower()
        best: tuple[int, Client] | None = None
        ambiguous = False
        for c in self.all():
            if any(a.lower() == exact for a in c.aliases):
                return c
            score = len(want & {a.lower() for a in c.aliases if " " not in a})  # distinctive tokens only
            if score and (best is None or score > best[0]):
                best, ambiguous = (score, c), False
            elif score and best is not None and score == best[0]:
                ambiguous = True
        return None if best is None or ambiguous else best[1]
