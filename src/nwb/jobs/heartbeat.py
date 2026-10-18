"""Heartbeat (OpenClaw gateway/heartbeat.md, Hermes wake gate): a periodic, quiet check.

Each tick, per person with an open plan:
  active hours (from config, in the agent timezone) ->
  wake gate (cheap checks; no model call when idle or unchanged) ->
  compose (template, or the background model only when the gate wakes) ->
  drop NO_REPLY / HEARTBEAT_OK ->
  deliver to that person's own DM (never a group) ->
  dedupe inside the configured window.
No bootstrap and no chat history on heartbeat runs (OpenClaw bootstrap-mode: heartbeat/cron never bootstrap).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

from nwb.actions.service import ActionService
from nwb.conf.loader import ConfigLoader
from nwb.conf.models import HeartbeatConfig, WakeCheckConfig
from nwb.db.repo_directory import DirectoryReader
from nwb.db.repo_jobs import JobRepository
from nwb.db.repo_onboarding import InboxRepository, OnboardingRepository
from nwb.db.repo_sessions import new_id
from nwb.errors import ConfigError
from nwb.obs.log import Hop, dataflow, get_logger, traced
from nwb.onboarding.models import BriefItem
from nwb.onboarding.service import OnboardingService
from nwb.ports import SqlDatabase
from nwb.prompts import PromptLibrary

log = get_logger(__name__)

Composer = Callable[[str], str]  # prompt -> text (background LLM); returns the silence token to stay quiet


@dataclass
class HeartbeatReport:
    at: datetime
    skipped: str | None = None
    sent: list[str] = field(default_factory=list)
    silent: list[str] = field(default_factory=list)
    deduped: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WakeSignal:
    """One thing the wake gate found. ``key`` is the stable id used for the unchanged check.

    ``item_id`` is set when the signal is an onboarding item (the nudges table references that id).
    Other signals, such as an approval, leave it empty. ``notice_title`` is the inbox title for this probe.
    """

    key: str
    title: str
    lead: str
    item_id: str | None = None
    notice_title: str = ""


@dataclass(frozen=True, slots=True)
class ProbeDeps:
    """What a wake probe may read. Every probe is constructed from this, so the registry stays one dict."""

    onboarding: OnboardingService
    actions: ActionService | None


class WakeProbe(Protocol):
    notice_title: str

    def __init__(self, deps: ProbeDeps) -> None: ...

    def collect(self, person_id: str, asof: date, within_days: int) -> list[WakeSignal]: ...


class OnboardingDueProbe:
    """Overdue onboarding items, plus items due within ``within_days``."""

    notice_title = "Onboarding reminder"

    def __init__(self, deps: ProbeDeps) -> None:
        self._ob = deps.onboarding

    def collect(self, person_id: str, asof: date, within_days: int) -> list[WakeSignal]:
        overdue, upcoming = self._ob.open_items(person_id, asof)
        soon = [item for item in upcoming if (item.due_date - asof).days <= within_days]
        return [_item_signal(item, self.notice_title) for item in (*overdue, *soon)]


class PendingApprovalsProbe:
    """Approvals waiting on this person. ``within_days`` does not apply."""

    notice_title = "Approval waiting"

    def __init__(self, deps: ProbeDeps) -> None:
        if deps.actions is None:
            raise ConfigError("heartbeat wake check 'pending_approvals' needs an ActionService")
        self._actions = deps.actions

    def collect(self, person_id: str, asof: date, within_days: int) -> list[WakeSignal]:
        del asof, within_days
        return [
            WakeSignal(
                key=view.action_id,
                title=view.title,
                lead="is waiting for your approval",
                item_id=None,
                notice_title=self.notice_title,
            )
            for view in self._actions.pending_for(person_id)
        ]


# The only list of wake-check names. Config stores which of these to run; it does not repeat the names.
PROBES: dict[str, type[WakeProbe]] = {
    "onboarding_due": OnboardingDueProbe,
    "pending_approvals": PendingApprovalsProbe,
}


def _item_signal(item: BriefItem, notice_title: str) -> WakeSignal:
    lead = "is overdue" if item.overdue else f"is due {item.due_date:%d %b}"
    return WakeSignal(key=item.id, title=item.title, lead=lead, item_id=item.id, notice_title=notice_title)


def _hm(value: str) -> time:
    parsed = time.fromisoformat(value)
    return time(parsed.hour, parsed.minute)


def _as_local(now: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def _tokens(cfg: HeartbeatConfig) -> tuple[str, ...]:
    return tuple(token for token in (cfg.silence_token.strip(), cfg.ack_token.strip()) if token)


def _bare(text: str) -> str:
    return text.strip().strip("\"'`*_").strip().strip("\"'`*_").strip()


def deliverable_text(text: str, cfg: HeartbeatConfig) -> str | None:
    """The message to send, or None when the reply is silence and must not be delivered.

    OpenClaw: an exact silence token is dropped. ``HEARTBEAT_OK`` (the ack token) at the start or end
    of a short reply drops the whole reply. A longer reply keeps its wording and loses the token.
    """
    raw = text.strip()
    if not raw:
        return None
    tokens = _tokens(cfg)
    body = _bare(raw)
    if body in tokens:
        return None
    for token in tokens:
        if body.startswith(token):
            rest = body[len(token) :].strip(" \t\r\n:.-")
        elif body.endswith(token):
            rest = body[: -len(token)].strip(" \t\r\n:.-")
        else:
            continue
        if len(rest) <= cfg.max_chars:
            return None
        body = rest
        break
    for token in tokens:
        body = body.replace(token, " ")
    body = " ".join(body.split())
    if not body or body in tokens:
        return None
    return body[: cfg.max_chars]


class HeartbeatJob:
    def __init__(
        self,
        db: SqlDatabase,
        onboarding: OnboardingService,
        prompts: PromptLibrary,
        cfg: HeartbeatConfig,
        composer: Composer | None = None,
        *,
        actions: ActionService | None = None,
        timezone: str = "UTC",
        loader: ConfigLoader | None = None,
    ) -> None:
        self._db = db
        self._ob = onboarding
        self._prompts = prompts
        self._compose = composer
        self._actions = actions
        self._tz = timezone
        self._loader = loader
        self._checks: list[tuple[WakeCheckConfig, WakeProbe]] = []
        self._apply(cfg)

    def _apply(self, cfg: HeartbeatConfig) -> None:
        unknown = [check.name for check in cfg.wake_gate.checks if check.name not in PROBES]
        if unknown:
            raise ConfigError(f"unknown heartbeat wake checks: {unknown}")
        self._cfg = cfg
        deps = ProbeDeps(self._ob, self._actions)
        self._checks = [(check, PROBES[check.name](deps)) for check in cfg.wake_gate.checks]

    def _refresh_config(self) -> None:
        if self._loader is None:
            return
        self._loader.refresh(("heartbeat",))
        self._apply(self._loader.load("heartbeat", HeartbeatConfig))

    def _hours_skip(self, now: datetime) -> str | None:
        local = _as_local(now, self._tz)
        if self._cfg.weekdays_only and local.weekday() >= 5:
            return "outside active hours (weekend)"
        if not (_hm(self._cfg.active_hours.start) <= local.time() < _hm(self._cfg.active_hours.end)):
            return "outside active hours"
        return None

    def in_active_hours(self, now: datetime) -> bool:
        return self._hours_skip(now) is None

    @traced(step="heartbeat.tick", level=logging.INFO)
    def tick(self, now: datetime, asof: date | None = None) -> HeartbeatReport:
        """One heartbeat. ``asof`` lets demos run a persona's day; defaults to ``now``'s date."""
        self._refresh_config()
        report = HeartbeatReport(at=now)
        if not self._cfg.enabled:
            report.skipped = "disabled"
            return report
        if reason := self._hours_skip(now):
            report.skipped = reason
            dataflow(Hop.JOB, job="heartbeat", skipped=report.skipped)
            return report
        day = asof or _as_local(now, self._tz).date()
        with self._db.unit_of_work() as uow:
            people = OnboardingRepository(uow).people_with_open_plans()
        for pid in people:
            self._one(pid, day, now, report)
        dataflow(Hop.JOB, job="heartbeat", sent=report.sent, silent=len(report.silent), deduped=len(report.deduped))
        return report

    def _one(self, person_id: str, asof: date, now: datetime, report: HeartbeatReport) -> None:
        signals = self._signals(person_id, asof)
        if not signals:
            report.silent.append(person_id)
            log.info("heartbeat wake gate", person=person_id, wake=False, llm=False, reason="idle")
            return
        fingerprint = ",".join(sorted({signal.key for signal in signals}))
        window = int(now.timestamp() // (self._cfg.dedupe_hours * 3600))
        key = "hb:" + hashlib.sha256(f"{person_id}|{window}|{fingerprint}".encode()).hexdigest()[:24]
        if self._cfg.wake_gate.skip_when_unchanged and self._already(key):
            report.deduped.append(person_id)
            log.info("heartbeat wake gate", person=person_id, wake=False, llm=False, reason="unchanged")
            return
        text = self._text(person_id, signals)
        body = deliverable_text(text, self._cfg)
        if body is None:
            self._record_silence(person_id, key)
            report.silent.append(person_id)
            log.info("heartbeat silence dropped", person=person_id)
            return
        self._deliver(person_id, body, signals, key, report)

    def _signals(self, person_id: str, asof: date) -> list[WakeSignal]:
        found: list[WakeSignal] = []
        seen: set[str] = set()
        for check, probe in self._checks:
            for signal in probe.collect(person_id, asof, check.within_days):
                if signal.key in seen:
                    continue
                seen.add(signal.key)
                found.append(signal)
        return found

    def _already(self, key: str) -> bool:
        """True when this fingerprint was already delivered, or the model already answered with silence."""
        with self._db.unit_of_work() as uow:
            if uow.fetchone("SELECT 1 AS ok FROM inbox_messages WHERE dedupe_key = :k", {"k": key}):
                return True
            row = uow.fetchone("SELECT status FROM job_runs WHERE idempotency_key = :k", {"k": key})
            return row is not None and str(row["status"]) == "skipped"

    def _record_silence(self, person_id: str, key: str) -> None:
        """Keep a dropped reply so the next tick in this window does not call the model again.

        The row is a skipped job run, not an inbox message: silence is never shown to the person.
        """
        with self._db.unit_of_work() as uow:
            jobs = JobRepository(uow)
            run_id = jobs.claim(jobs.ensure_job("heartbeat", "heartbeat"), key, "person", person_id)
            if run_id is not None:
                jobs.finish(run_id, "skipped")

    def _deliver(self, person_id: str, body: str, signals: list[WakeSignal], key: str, report: HeartbeatReport) -> None:
        # delivery.to is "owner": the row is this person's DM. There is no group recipient.
        if self._cfg.delivery.to != "owner":
            raise ConfigError("heartbeat delivers to the owner only")
        channel = self._cfg.delivery.channel
        notice = signals[0]
        with self._db.unit_of_work(as_person=person_id) as uow:
            inserted = InboxRepository(uow).add(
                new_id("ib"), person_id, "nudge", notice.notice_title, body, notice.item_id, key
            )
            if inserted:
                for i, signal in enumerate(signals):
                    uow.execute(
                        "INSERT INTO nudges (id, person_id, item_id, channel, dedupe_key) "
                        "VALUES (:id, :p, :i, :ch, :k)",
                        {"id": new_id("nd"), "p": person_id, "i": signal.item_id, "ch": channel, "k": f"{key}:{i}"},
                    )
        (report.sent if inserted else report.deduped).append(person_id)

    def _text(self, person_id: str, signals: list[WakeSignal]) -> str:
        with self._db.unit_of_work() as uow:
            me = DirectoryReader(uow).person(person_id)
        name = me.name.split()[0] if me else person_id
        lines = [f"{signal.title} {signal.lead}" for signal in signals]
        if self._cfg.use_llm and self._compose is not None:  # use_llm is re-read from heartbeat.yaml each tick
            prompt = self._prompts.render(
                self._cfg.prompt,
                name=name,
                items=lines,
                max_chars=self._cfg.max_chars,
                silence_token=self._cfg.silence_token,
            )
            try:
                return self._compose(prompt.text)
            except Exception:  # the template below is always available; a nudge never depends on the LLM
                log.exception("heartbeat composer failed; using template")
        first = signals[0]
        more = len(signals) - 1
        if more <= 0:
            tail = ""
        elif all(signal.item_id for signal in signals):
            tail = f" (+{more} more in your onboarding brief)"
        else:
            tail = f" (+{more} more)"
        return f"{name}, '{first.title}' {first.lead}{tail}. Ask me for your brief any time."
