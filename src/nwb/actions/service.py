"""ActionService: propose -> (approval) -> execute. One path for agent-initiated and watcher-initiated actions.

Approver resolution and executors are chosen by ``config/actions.yaml``; nothing executes without an approval row
decided by the resolved approver, except ``approver: self`` kinds, whose approval is the in-chat interrupt.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Protocol

from nwb.actions.models import ActionView, Decision
from nwb.conf.models import ActionKind, ActionsConfig
from nwb.db.repo_directory import DirectoryReader
from nwb.db.repo_onboarding import InboxRepository
from nwb.db.repo_sessions import new_id
from nwb.errors import AccessDeniedError, ConflictError, NotFoundError
from nwb.obs.log import Hop, dataflow
from nwb.ports import Row, SqlDatabase, UnitOfWork


class Executor(Protocol):
    def __call__(self, uow: UnitOfWork, action: ActionView) -> str: ...


def inbox_executor(uow: UnitOfWork, action: ActionView) -> str:
    """Deliver the action's message to the target's in-app inbox. Returns a result note."""
    target = action.target or action.requester
    msg = str(action.args.get("message") or action.title)
    InboxRepository(uow).add(
        new_id("ib"), target, "system", action.title, msg, action.action_id, f"act:{action.action_id}"
    )
    return f"delivered to {target}'s inbox"


EXECUTORS: dict[str, Executor] = {"inbox": inbox_executor}


class ActionService:
    def __init__(self, db: SqlDatabase, cfg: ActionsConfig, executors: dict[str, Executor] | None = None) -> None:
        self._db = db
        self._cfg = cfg
        self._executors = executors or EXECUTORS

    def bind_config(self, cfg: ActionsConfig) -> None:
        """Switch to a config just reloaded from disk. Kinds and approvers follow the new file."""
        self._cfg = cfg

    def kind(self, name: str) -> ActionKind:
        k = self._cfg.kinds.get(name)
        if k is None:
            raise NotFoundError(f"unknown action kind {name!r}; known: {', '.join(sorted(self._cfg.kinds))}")
        return k

    # ------------------------------------------------------------------ approver
    def _approver(
        self, uow: UnitOfWork, kind: ActionKind, requester: str, target: str | None, args: dict[str, Any], asof: date
    ) -> str | None:
        if kind.approver == "self":
            return requester
        d = DirectoryReader(uow)
        if kind.approver == "manager_of_target":
            mgr = d.manager(target or requester, asof)
            return mgr.id if mgr else None
        rid = str(args.get("resource_id") or "")
        row = uow.fetchone("SELECT owner_person_id FROM documents WHERE id = :id", {"id": rid}) or uow.fetchone(
            "SELECT owner_person_id FROM records WHERE id = :id", {"id": rid}
        )
        return str(row["owner_person_id"]) if row and row["owner_person_id"] else None

    # ------------------------------------------------------------------ propose
    def propose(
        self,
        *,
        kind_name: str,
        requester: str,
        target: str | None,
        title: str,
        args: dict[str, Any],
        asof: date,
        proposed_by: str,
        run_id: str | None = None,
        dedupe_key: str | None = None,
        pre_approved: bool = False,
        approver_id: str | None = None,
    ) -> ActionView:
        """Create the action. ``pre_approved`` is only honoured for ``approver: self`` (the chat interrupt).

        ``approver_id`` is only read when the kind's approver is ``configured`` (a system job
        already resolved the person from config). The chat tool does not pass it.
        """
        kind = self.kind(kind_name)
        aid = new_id("act")
        with self._db.unit_of_work(as_person=requester) as uow:
            if dedupe_key and (dup := uow.fetchone("SELECT id FROM actions WHERE dedupe_key = :k", {"k": dedupe_key})):
                return self._view(uow, str(dup["id"]), note="already proposed")
            if target and DirectoryReader(uow).person(target) is None:
                raise NotFoundError(f"no person {target}")
            approver = (
                approver_id
                if kind.approver == "configured"
                else self._approver(uow, kind, requester, target, args, asof)
            )
            if approver is None:
                raise NotFoundError(f"no approver can be resolved for {kind_name}")
            uow.execute(
                "INSERT INTO actions (id, agent_run_id, person_id, action_type, args, status, policy_ref, dedupe_key, "
                "proposed_by, title) VALUES (:id, :run, :p, :t, :a, 'awaiting_approval', :pol, :k, :by, :title)",
                {
                    "id": aid,
                    "run": run_id,
                    "p": requester,
                    "t": kind_name,
                    "a": json.dumps({**args, "target": target}),
                    "pol": f"config/actions.yaml#{kind_name}",
                    "k": dedupe_key,
                    "by": proposed_by,
                    "title": title,
                },
            )
            apid = new_id("apr")
            uow.execute(
                "INSERT INTO approvals (id, action_id, approver_id) VALUES (:id, :a, :p)",
                {"id": apid, "a": aid, "p": approver},
            )
            if not (pre_approved and kind.approver == "self"):
                InboxRepository(uow).add(
                    new_id("ib"),
                    approver,
                    "approval",
                    f"Approval needed: {title}",
                    str(args.get("message") or kind.description),
                    apid,
                    f"apr:{apid}",
                )
        dataflow(Hop.TOOL_CALL, kind="action_proposed", action=aid, type=kind_name, approver=approver, by=proposed_by)
        if pre_approved and kind.approver == "self":
            return self.decide(requester, apid, "approve", "approved in chat")
        with self._db.unit_of_work(as_person=requester) as uow:
            return self._view(uow, aid)

    # ------------------------------------------------------------------ decide
    def decide(self, approver_id: str, approval_id: str, decision: Decision, comment: str | None) -> ActionView:
        with self._db.unit_of_work(as_person=approver_id) as uow:
            ap = uow.fetchone(
                "SELECT id, action_id, approver_id, status FROM approvals WHERE id = :id", {"id": approval_id}
            )
            if ap is None:
                raise NotFoundError(f"no approval {approval_id}")
            if str(ap["approver_id"]) != approver_id:
                raise AccessDeniedError("only the assigned approver can decide this")
            if ap["status"] != "pending":
                raise ConflictError(f"approval already {ap['status']}")
            status = "approved" if decision == "approve" else "rejected"
            uow.execute(
                "UPDATE approvals SET status = :s, comment = :c, decided_at = CURRENT_TIMESTAMP WHERE id = :id",
                {"s": status, "c": comment, "id": approval_id},
            )
            action_id = str(ap["action_id"])
            uow.execute("UPDATE actions SET status = :s WHERE id = :id", {"s": status, "id": action_id})
            view = self._view(uow, action_id)
            note = None
            if status == "approved":
                executor = self._executors[self.kind(view.kind).executor]
                note = executor(uow, view)
                uow.execute(
                    "UPDATE actions SET status = 'done', done_at = CURRENT_TIMESTAMP WHERE id = :id", {"id": action_id}
                )
                view = self._view(uow, action_id, note=note)
        dataflow(Hop.TOOL_CALL, kind="action_decided", action=action_id, decision=decision, by=approver_id, note=note)
        return view

    # ------------------------------------------------------------------ read
    def pending_for(self, approver_id: str) -> list[ActionView]:
        with self._db.unit_of_work(as_person=approver_id) as uow:
            rows = uow.fetchall(
                "SELECT action_id FROM approvals WHERE approver_id = :p AND status = 'pending' ORDER BY id",
                {"p": approver_id},
            )
            return [self._view(uow, str(r["action_id"])) for r in rows]

    def _view(self, uow: UnitOfWork, action_id: str, note: str | None = None) -> ActionView:
        a: Row | None = uow.fetchone("SELECT * FROM actions WHERE id = :id", {"id": action_id})
        if a is None:
            raise NotFoundError(f"no action {action_id}")
        ap = uow.fetchone(
            "SELECT id, approver_id FROM approvals WHERE action_id = :id ORDER BY step", {"id": action_id}
        )
        args = json.loads(str(a["args"] or "{}"))
        return ActionView(
            action_id=action_id,
            approval_id=str(ap["id"]) if ap else None,
            kind=str(a["action_type"]),
            title=str(a["title"] or a["action_type"]),
            status=str(a["status"]),
            requester=str(a["person_id"]),
            approver=str(ap["approver_id"]) if ap else None,
            target=args.pop("target", None),
            args=args,
            proposed_by=str(a["proposed_by"]),
            note=note,
        )
