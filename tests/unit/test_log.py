"""nwb.obs.log: the one logging module (setup per env, structured fields, steps, hops, redaction, quiet libs)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from nwb.config import Settings
from nwb.obs.log import (
    Hop,
    dataflow,
    get_logger,
    log_step,
    new_trace,
    scrub,
    scrub_str,
    setup_logging,
    traced,
)

ENV = ("APP_ENV", "LOG_LEVEL", "LOG_FORMAT", "LOG_FILE", "LOG_QUIET", "DATAFLOW_TRACE", "DATABASE_URL")


def mk(monkeypatch: pytest.MonkeyPatch, tmp: Path, **env: str) -> Settings:
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    s.log_dir = tmp
    return s


@pytest.fixture(autouse=True)
def _reset_handlers() -> Iterator[None]:
    yield
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_nwb_handler", False)]:
        root.removeHandler(h)
        h.close()


def _lines(p: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_dev_console_plus_app_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    done = setup_logging(mk(monkeypatch, tmp_path))
    assert (done.env, done.level, done.console) == ("dev", "DEBUG", "console")
    assert done.files == (tmp_path / "app.jsonl", tmp_path / "dataflow.jsonl")
    with new_trace("t-dev"):
        get_logger("nwb.test").info("identity resolved", person="P1", api_key="gsk_abcdefghijk")
    rec = next(r for r in _lines(tmp_path / "app.jsonl") if r["msg"] == "identity resolved")
    assert (rec["person"], rec["api_key"], rec["trace_id"], rec["level"]) == ("P1", "[redacted]", "t-dev", "INFO")


def test_prod_is_info_json_stdout_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    done = setup_logging(mk(monkeypatch, tmp_path, APP_ENV="prod"))
    assert (done.level, done.console, done.files) == ("INFO", "json", ())
    log = get_logger("nwb.test")
    log.debug("hidden in prod")
    log.info("response out", status=200, content="the answer")
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [o["msg"] for o in out] == ["response out"]
    assert (out[0]["status"], out[0]["content"]) == (200, "[10 chars]")
    assert not any(tmp_path.iterdir())


def test_setup_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = mk(monkeypatch, tmp_path)
    setup_logging(s)
    setup_logging(s)
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_nwb_handler", False)]
    assert len(ours) == 3  # console + app.jsonl + dataflow.jsonl, not doubled


def test_noisy_libraries_quieted_from_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup_logging(mk(monkeypatch, tmp_path, LOG_QUIET="litellm,some.lib"))
    assert logging.getLogger("litellm").level == logging.WARNING
    assert logging.getLogger("some.lib").level == logging.WARNING
    default = mk(monkeypatch, tmp_path)
    assert {"litellm", "httpx", "urllib3"} <= set(default.log_quiet.split(","))


def test_log_step_logs_end_and_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup_logging(mk(monkeypatch, tmp_path))
    with log_step("ingest", level=logging.INFO, source="data") as step:
        step.set(rows=3)
    with pytest.raises(ValueError, match="bad row"), log_step("ingest", source="data"):
        raise ValueError("bad row")
    recs = [r for r in _lines(tmp_path / "app.jsonl") if r.get("step") == "ingest"]
    assert [r["msg"] for r in recs] == [
        "ingest start",
        "ingest done",
        "ingest start",
        "ingest failed: ValueError: bad row",
    ]
    assert (recs[1]["rows"], recs[1]["source"], recs[3]["level"], recs[3]["error"]) == (
        3,
        "data",
        "ERROR",
        "ValueError",
    )
    assert isinstance(recs[1]["ms"], int)


def test_traced_decorator_names_the_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup_logging(mk(monkeypatch, tmp_path))

    class Svc:
        @traced
        def lookup(self, q: str) -> str:
            return q.upper()

        @traced(step="svc.fail", level=logging.INFO)
        def fail(self) -> None:
            raise RuntimeError("nope")

    assert Svc().lookup("x") == "X"
    with pytest.raises(RuntimeError):
        Svc().fail()
    msgs = [r["msg"] for r in _lines(tmp_path / "app.jsonl") if "step" in r]
    assert msgs == [
        "test_traced_decorator_names_the_step.<locals>.Svc.lookup start",
        "test_traced_decorator_names_the_step.<locals>.Svc.lookup done",
        "svc.fail start",
        "svc.fail failed: RuntimeError: nope",
    ]


def test_dataflow_milestones_info_chatty_debug(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup_logging(mk(monkeypatch, tmp_path))
    dataflow(Hop.TOOL_CALL, tool="company_fact", ms=12)
    dataflow(Hop.SQL_WRITE, table="people", rows=2)
    levels = {r["hop"]: r["level"] for r in _lines(tmp_path / "dataflow.jsonl")}
    assert levels == {"tool.call": "INFO", "sql.write": "DEBUG"}


def test_redaction_covers_keys_and_url_credentials() -> None:
    assert scrub_str("Bearer sk-or-v1-abcdef0123456789") == "Bearer [redacted]"
    assert scrub_str("redis://falkor:s3cret@host:6379") == "redis://[redacted]@host:6379"
    assert scrub({"password": "x", "nested": {"token": "y", "n": 1}}, keep_content=True) == {
        "password": "[redacted]",
        "nested": {"token": "[redacted]", "n": 1},
    }


def test_fields_never_overwrite_record_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setup_logging(mk(monkeypatch, tmp_path))
    get_logger("nwb.test").warning("odd", level="custom", msg="m")
    rec = next(r for r in _lines(tmp_path / "app.jsonl") if r["msg"] == "odd")
    assert (rec["level"], rec["field_level"], rec["field_msg"]) == ("WARNING", "custom", "m")


def test_struct_logger_keeps_percent_args(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="nwb.test"):
        get_logger("nwb.test").info("run %s failed", "r1", run="r1")
    rec = caplog.records[-1]
    assert rec.getMessage() == "run r1 failed"
    assert rec.fields == {"run": "r1"}  # type: ignore[attr-defined]
