"""The one logging module. Everything that logs imports from here.

    from nwb.obs.log import get_logger, log_step, traced, dataflow, Hop
    log = get_logger(__name__)
    log.info("identity resolved", person=pid)        # keyword args become structured fields

Set up once per process at the entry point (``nwb`` CLI, API startup, scripts) with ``setup_logging(settings)``.

What goes where (driven by ``Settings``: APP_ENV, LOG_LEVEL, LOG_FORMAT, LOG_FILE, DATAFLOW_TRACE, LOG_QUIET):

- dev  -> DEBUG, readable console on stderr, plus ``var/logs/app.jsonl`` (every record, JSON) and
          ``var/logs/dataflow.jsonl`` (only the data-flow hops, content kept: local files only).
- prod -> INFO, JSON on stdout, never content bodies, no files.

Secrets are scrubbed from every record in every mode (key names like ``api_key``/``password``, values that look like
provider keys, and ``user:pass@`` in URLs). Noisy libraries (``LOG_QUIET``) are held at WARNING.

``dataflow(Hop.X, **fields)`` is the data-flow trace: one event per hop of a request (api -> session -> agent.run ->
agent.payload -> tool.call -> retrieval / memory.write -> ...). Milestone hops log at INFO so they show in every mode;
chatty ones (sql.write, llm.call, outbox, migrate) at DEBUG.

``log_step("name")`` (context manager or decorator) and ``@traced`` log start, end, duration and failure of a step.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import ContextDecorator, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nwb.config import Settings

__all__ = [
    "Hop",
    "LogSetup",
    "StructLogger",
    "asdict_safe",
    "dataflow",
    "get_logger",
    "log_step",
    "new_trace",
    "same_trace",
    "scrub",
    "scrub_str",
    "setup_logging",
    "trace_id",
    "traced",
]

# ======================================================================== trace ids (one per request / turn / command)
_trace: ContextVar[str] = ContextVar("nwb_trace_id", default="-")


def trace_id() -> str:
    return _trace.get()


@contextmanager
def new_trace(tid: str | None = None) -> Iterator[str]:
    token = _trace.set(tid or uuid.uuid4().hex[:16])
    try:
        yield _trace.get()
    finally:
        _trace.reset(token)


@contextmanager
def same_trace() -> Iterator[str]:
    """Reuse the trace already bound on this context.

    The HTTP middleware opens one id per request. The agent worker re-binds that same id,
    and the runner must not mint a second one. A caller with no trace (CLI, a direct test)
    still gets a fresh id.
    """
    current = _trace.get()
    if current != "-":
        yield current
        return
    with new_trace() as tid:
        yield tid


# ======================================================================== redaction
SECRET_KEY = re.compile(r"(pass(word)?|secret|token|api[_-]?key|authorization|dsn|cookie)", re.I)
SECRET_VALUE = re.compile(
    r"(gsk_[A-Za-z0-9]{8,}|nvapi-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,}"
    r"|(?<=://)[^/\s:@]+:[^/\s@]+(?=@))"
)
CONTENT_KEYS = frozenset({"content", "body", "text", "prompt", "completion", "message", "messages", "email", "name"})
REDACTED = "[redacted]"


def scrub_str(s: str) -> str:
    """Mask provider keys and URL passwords inside free text."""
    return SECRET_VALUE.sub(REDACTED, s)


def scrub(data: Mapping[str, Any], *, keep_content: bool) -> dict[str, Any]:
    """Mask secret-named keys always; replace content bodies with their length unless ``keep_content``."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if SECRET_KEY.search(k):
            out[k] = REDACTED
        elif not keep_content and k in CONTENT_KEYS:
            out[k] = f"[{len(str(v))} chars]"
        elif isinstance(v, Mapping):
            out[k] = scrub(v, keep_content=keep_content)
        elif isinstance(v, str):
            out[k] = scrub_str(v)
        else:
            out[k] = v
    return out


# ======================================================================== loggers
class StructLogger(logging.LoggerAdapter[logging.Logger]):
    """``log.info("msg", key=value)``: keyword arguments become structured fields (JSON keys / ``k=v`` on console).

    The standard keywords (``exc_info``, ``stack_info``, ``stacklevel``, ``extra``) keep their usual meaning and
    ``%s`` arguments still work, so this is a drop-in for ``logging.getLogger``.
    """

    _STD = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})

    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> tuple[Any, MutableMapping[str, Any]]:
        extra_fields = {k: kwargs.pop(k) for k in list(kwargs) if k not in self._STD}
        extra = dict(kwargs.get("extra") or {})
        if extra_fields:
            extra["fields"] = {**extra.get("fields", {}), **extra_fields}
        kwargs["extra"] = extra
        return msg, kwargs

    # Positional-only ``level``/``msg`` so fields may use those names too (``log.info("ready", level="DEBUG")``).
    def log(self, level: int, msg: Any, /, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(level):
            msg, kwargs = self.process(msg, kwargs)
            kwargs.setdefault("stacklevel", 2)
            kwargs["stacklevel"] += 1
            self.logger.log(level, msg, *args, **kwargs)

    def debug(self, msg: Any, /, *args: Any, **kwargs: Any) -> None:
        self.log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: Any, /, *args: Any, **kwargs: Any) -> None:
        self.log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: Any, /, *args: Any, **kwargs: Any) -> None:
        self.log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: Any, /, *args: Any, **kwargs: Any) -> None:
        self.log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: Any, /, *args: Any, exc_info: Any = True, **kwargs: Any) -> None:
        self.log(logging.ERROR, msg, *args, exc_info=exc_info, **kwargs)

    def critical(self, msg: Any, /, *args: Any, **kwargs: Any) -> None:
        self.log(logging.CRITICAL, msg, *args, **kwargs)


def get_logger(name: str) -> StructLogger:
    return StructLogger(logging.getLogger(name), {})


# ======================================================================== data-flow hops
class Hop(StrEnum):
    INGEST = "ingest"
    SQL_WRITE = "sql.write"
    MIGRATE = "migrate"
    OUTBOX = "outbox"
    GRAPH_SYNC = "graph.sync"
    RETRIEVAL = "retrieval"
    LLM_CALL = "llm.call"
    LLM_FALLBACK = "llm.fallback"
    TOOL_CALL = "tool.call"
    MEMORY_WRITE = "memory.write"
    PAYLOAD = "agent.payload"
    AGENT_RUN = "agent.run"
    SESSION = "session"
    JOB = "job"
    API = "api"


_CHATTY_HOPS = frozenset({Hop.SQL_WRITE, Hop.MIGRATE, Hop.OUTBOX, Hop.LLM_CALL})
_MILESTONE_HOPS = frozenset(Hop) - _CHATTY_HOPS
_dataflow_log = logging.getLogger("nwb.dataflow")


def dataflow(hop: Hop | str, **data: Any) -> None:
    """One structured event per hop. Milestones at INFO, chatty hops (and ad-hoc string hops) at DEBUG."""
    level = logging.INFO if hop in _MILESTONE_HOPS else logging.DEBUG
    _dataflow_log.log(level, str(hop), extra={"hop": str(hop), "fields": data})


def asdict_safe(obj: Any) -> dict[str, Any]:
    """Scalar fields of a report dataclass, for logging."""
    if not is_dataclass(obj):
        return {}
    return {f.name: v for f in fields(obj) if isinstance(v := getattr(obj, f.name), int | float | str | bool)}


# ======================================================================== steps: start / end / duration / failure
_step_log = get_logger("nwb.step")


class log_step(ContextDecorator):  # noqa: N801 - reads as a verb at call sites: ``with log_step("ingest")``
    """Log a step's start (DEBUG), end with duration (``level``, DEBUG by default) and failure (ERROR).

        with log_step("layer2.load", source="data/") as step:
            ...
            step.set(records=12)        # extra fields on the end line

        @log_step("graph.drain")
        def drain(): ...

    Exceptions are logged with type and message, then re-raised (never swallowed).
    """

    def __init__(self, step: str, *, logger: StructLogger | None = None, level: int = logging.DEBUG, **data: Any):
        self.step, self._log, self._level, self._data = step, logger or _step_log, level, data
        self._t0 = 0.0
        self._extra: dict[str, Any] = {}

    def _recreate_cm(self) -> log_step:  # a fresh instance per decorated call: thread- and re-entrancy-safe
        return log_step(self.step, logger=self._log, level=self._level, **self._data)

    def set(self, **data: Any) -> None:
        self._extra.update(data)

    def __enter__(self) -> log_step:
        self._t0 = time.perf_counter()
        self._log.debug(f"{self.step} start", step=self.step, **self._data)
        return self

    def __exit__(self, et: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> bool:
        ms = int((time.perf_counter() - self._t0) * 1000)
        if exc is None:
            self._log.log(self._level, f"{self.step} done", step=self.step, ms=ms, **self._data, **self._extra)
        else:
            self._log.error(
                f"{self.step} failed: {type(exc).__name__}: {exc}"[:500],
                step=self.step,
                ms=ms,
                error=type(exc).__name__,
                **self._data,
            )
        return False


def traced[**P, R](fn: Callable[P, R] | None = None, *, step: str | None = None, level: int = logging.DEBUG) -> Any:
    """``@traced`` or ``@traced(step="name")``: ``log_step`` named after the function (``Class.method``)."""

    def wrap(f: Callable[P, R]) -> Callable[P, R]:
        name = step or f.__qualname__
        logger = get_logger(f.__module__)

        @functools.wraps(f)
        def inner(*args: P.args, **kwargs: P.kwargs) -> R:
            with log_step(name, logger=logger, level=level):
                return f(*args, **kwargs)

        return inner

    return wrap(fn) if fn is not None else wrap


# ======================================================================== formatters
class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id()
        return True


_RESERVED = frozenset({"ts", "level", "logger", "trace_id", "msg", "hop", "exc"})


class JsonFormatter(logging.Formatter):
    def __init__(self, *, keep_content: bool) -> None:
        super().__init__()
        self._keep = keep_content

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", "-"),
            "msg": scrub_str(record.getMessage()),
        }
        if hop := getattr(record, "hop", None):
            doc["hop"] = hop
        if data := getattr(record, "fields", None):
            for k, v in scrub(data, keep_content=self._keep).items():
                doc[f"field_{k}" if k in _RESERVED else k] = v  # a field never overwrites ts/level/msg/...
        if record.exc_info:
            doc["exc"] = scrub_str(self.formatException(record.exc_info))
        return json.dumps(doc, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """``10:42:07.123 INFO  [trace] nwb.agent.runner: agent.run event=start run=...`` (content shown as length)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        base = f"{ts} {record.levelname:<5} [{getattr(record, 'trace_id', '-')}] {record.name}: {record.getMessage()}"
        if data := getattr(record, "fields", None):
            base += " " + " ".join(f"{k}={_short(v)}" for k, v in scrub(data, keep_content=False).items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return scrub_str(base)


def _short(v: Any, limit: int = 160) -> str:
    s = str(v)
    return s if len(s) <= limit else s[: limit - 3] + "..."


# ======================================================================== setup (once per process, idempotent)
_HANDLER_TAG = "_nwb_handler"


@dataclass(frozen=True, slots=True)
class LogSetup:
    """What ``setup_logging`` did, so entry points can say where logs went."""

    env: str
    level: str
    console: str
    files: tuple[Path, ...]
    quiet: tuple[str, ...]


def _tag(h: logging.Handler) -> logging.Handler:
    setattr(h, _HANDLER_TAG, True)
    h.addFilter(_TraceFilter())
    return h


def _file(path: Path, level: int, keep_content: bool, only: str | None = None) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = _tag(logging.FileHandler(path, encoding="utf-8"))
    fh.setLevel(level)
    if only is not None:
        fh.addFilter(lambda r: r.name == only)
    fh.setFormatter(JsonFormatter(keep_content=keep_content))
    return fh


def setup_logging(s: Settings | None = None) -> LogSetup:
    """Configure the root logger from settings. Safe to call more than once (our handlers are replaced)."""
    if s is None:
        from nwb.config import get_settings

        s = get_settings()
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(h)
        h.close()
    level = logging.getLevelNamesMapping().get(s.log_level.upper(), logging.INFO)
    dev_files = not s.is_prod and bool(s.log_file)
    trace_file = not s.is_prod and bool(s.dataflow_trace)
    root.setLevel(logging.DEBUG if (trace_file or dev_files) else level)

    out = _tag(logging.StreamHandler(sys.stdout if s.is_prod else sys.stderr))
    out.setLevel(level)
    out.setFormatter(JsonFormatter(keep_content=False) if s.log_format == "json" else ConsoleFormatter())
    root.addHandler(out)

    files: list[Path] = []
    if dev_files:
        files.append(s.log_dir / "app.jsonl")
        root.addHandler(_file(files[-1], logging.DEBUG, keep_content=trace_file))
    if trace_file:
        files.append(s.log_dir / "dataflow.jsonl")
        root.addHandler(_file(files[-1], logging.DEBUG, keep_content=True, only="nwb.dataflow"))

    quiet = tuple(q.strip() for q in s.log_quiet.split(",") if q.strip())
    for name in quiet:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    done = LogSetup(s.app_env, logging.getLevelName(level), s.log_format, tuple(files), quiet)
    get_logger("nwb.obs").debug(
        "logging ready",
        env=done.env,
        log_level=done.level,
        console=done.console,
        files=[str(f) for f in done.files] or "-",
    )
    return done
