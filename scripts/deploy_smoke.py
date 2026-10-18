"""Hit a running Company Brain and exit non-zero unless health and the scripted requests pass.

    python scripts/deploy_smoke.py --base-url http://127.0.0.1:7860
    python scripts/deploy_smoke.py --serve-fake --host 127.0.0.1 --port 8765

``--serve-fake`` is an OpenAI-compatible stand-in for local smoke when no provider key is set.
It reads the same cases file as the client and calls the real tools (read the skill, then the tool).
Questions live in ``config/deploy_smoke.yaml``. The persona id comes from ``GET /personas``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from nwb.api.identity import DemoHeaderIdentity
from nwb.config import get_settings
from nwb.obs.log import get_logger, setup_logging

log = get_logger(__name__)

READ_LIMIT = 1000
_USER_ID = re.compile(r"user_id:\s*(\S+)")


@dataclass(frozen=True)
class Expect:
    tools_any: tuple[str, ...]
    status: str
    abstains: bool


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    tool: str
    arg: str
    arg_from: str
    skill: bool
    expect: Expect


@dataclass(frozen=True)
class SmokeConfig:
    abstain_reply: str
    abstain_patterns: tuple[str, ...]
    company_skill: str
    cases: tuple[Case, ...]


@dataclass(frozen=True)
class Action:
    kind: str  # "text" | "tool"
    text: str = ""
    tool: str = ""
    args: dict[str, Any] | None = None


def load_cases(path: Path) -> SmokeConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping")
    cases: list[Case] = []
    for item in raw.get("cases") or []:
        expect = item["expect"]
        cases.append(
            Case(
                id=str(item["id"]),
                question=str(item["question"]),
                tool=str(item["tool"]),
                arg=str(item["arg"]),
                arg_from=str(item["arg_from"]),
                skill=bool(item.get("skill")),
                expect=Expect(
                    tools_any=tuple(expect.get("tools_any") or ()),
                    status=str(expect.get("status") or "done"),
                    abstains=bool(expect.get("abstains")),
                ),
            )
        )
    if not cases:
        raise ValueError(f"{path} has no cases")
    return SmokeConfig(
        abstain_reply=str(raw.get("abstain_reply") or "I don't have that on record."),
        abstain_patterns=tuple(str(p).lower() for p in (raw.get("abstain_patterns") or ())),
        company_skill=str(raw.get("company_skill") or "/skills/company-answers/SKILL.md"),
        cases=tuple(cases),
    )


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _tool_names(tools: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            names.add(str(name))
    return names


def _called(messages: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else call
            name = fn.get("name") if isinstance(fn, dict) else None
            if name:
                names.add(str(name))
    return names


def _user_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(_text(m.get("content")) for m in messages if m.get("role") == "user")


def _user_id(messages: list[dict[str, Any]]) -> str:
    blob = "\n".join(_text(m.get("content")) for m in messages)
    found = _USER_ID.search(blob)
    return found.group(1) if found else ""


def _last_tool_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "tool":
            return _text(message.get("content"))
    return ""


def _match(user_text: str, cfg: SmokeConfig) -> Case | None:
    hits = [case for case in cfg.cases if case.question and case.question in user_text]
    if not hits:
        return None
    return max(hits, key=lambda case: len(case.question))


def _abstained(text: str, cfg: SmokeConfig) -> bool:
    low = text.lower()
    return any(pattern in low for pattern in cfg.abstain_patterns)


def plan(messages: list[dict[str, Any]], tool_names: set[str], cfg: SmokeConfig) -> Action:
    """Next model turn for the fake server. Real tools still run inside the app."""
    user_text = _user_text(messages)
    case = _match(user_text, cfg)
    if case is None:
        return Action(kind="text", text="OK")
    if not tool_names:
        return Action(kind="text", text=f"Summary. User asked: {user_text[:500]}")
    called = _called(messages)
    if case.skill and case.tool not in tool_names and "read_file" in tool_names and "read_file" not in called:
        return Action(kind="tool", tool="read_file", args={"file_path": cfg.company_skill, "limit": READ_LIMIT})
    if case.tool in tool_names and case.tool not in called:
        value = (_user_id(messages) or "unknown") if case.arg_from == "user_id" else case.question
        return Action(kind="tool", tool=case.tool, args={case.arg: value})
    tool_text = _last_tool_text(messages)
    if case.expect.abstains and (_abstained(tool_text, cfg) or not tool_text.strip()):
        return Action(kind="text", text=cfg.abstain_reply)
    if case.expect.abstains:
        return Action(kind="text", text=tool_text[:800])
    snippet = " ".join(tool_text.split())[:400]
    return Action(kind="text", text=f"Result: {snippet}" if snippet else "Result: empty tool output.")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    cfg: SmokeConfig
    calls: int = 0
    lock: threading.Lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("fake llm %s", fmt % args)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "local", "object": "model"}]}).encode()
            self._send(200, "application/json", body)
            return
        self._send(404, "application/json", b'{"error":"not_found"}')

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, "application/json", b'{"error":"not_found"}')
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, "application/json", b'{"error":"bad_json"}')
            return
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        action = plan(messages, _tool_names(payload.get("tools")), self.cfg)
        with self.lock:
            self.calls += 1
            call_id = f"call_{self.calls}"
        log.info("fake llm decision", tool=action.tool or "-", kind=action.kind, path=self.path)
        model = str(payload.get("model") or "local")
        stream = bool(payload.get("stream"))
        if stream:
            self._stream(model, call_id, action)
        else:
            self._send(200, "application/json", json.dumps(_completion(model, call_id, action)).encode())

    def _stream(self, model: str, call_id: str, action: Action) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        created = int(time.time())
        chunks: list[dict[str, Any]] = []
        if action.kind == "tool":
            chunks.append(
                _chunk(
                    model,
                    created,
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": action.tool, "arguments": ""},
                            }
                        ],
                    },
                )
            )
            chunks.append(
                _chunk(
                    model,
                    created,
                    {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(action.args or {})}}]},
                )
            )
            chunks.append(_chunk(model, created, {}, finish="tool_calls"))
        else:
            chunks.append(_chunk(model, created, {"role": "assistant", "content": action.text}))
            chunks.append(_chunk(model, created, {}, finish="stop"))
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _completion(model: str, call_id: str, action: Action) -> dict[str, Any]:
    if action.kind == "tool":
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": action.tool, "arguments": json.dumps(action.args or {})},
                }
            ],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": action.text}
        finish = "stop"
    return {
        "id": f"chatcmpl-{call_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _chunk(model: str, created: int, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-smoke",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def make_server(host: str, port: int, cfg: SmokeConfig) -> ThreadingHTTPServer:
    _Handler.cfg = cfg
    return ThreadingHTTPServer((host, port), _Handler)


def serve_fake(host: str, port: int, cfg: SmokeConfig) -> None:
    httpd = make_server(host, port, cfg)
    log.info("fake llm listening", host=host, port=port, cases=len(cfg.cases))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode(errors="replace")


def parse_sse(payload: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name = "message"
    data: list[str] = []
    for line in payload.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line.split(":", 1)[1].strip())
        elif line == "" and data:
            raw = "\n".join(data)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            events.append((name, parsed if isinstance(parsed, dict) else {"raw": parsed}))
            name, data = "message", []
    if data:
        raw = "\n".join(data)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        events.append((name, parsed if isinstance(parsed, dict) else {"raw": parsed}))
    return events


def _wait_health(base: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = "not ready"
    while time.monotonic() < deadline:
        try:
            status, text = _request(f"{base}/health", timeout=30)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
            log.info("health not ready", error=last)
            time.sleep(2)
            continue
        if status == 200:
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                last = "bad json"
            else:
                if isinstance(body, dict) and body.get("status") == "ok":
                    log.info("health ok", env=body.get("env"), checks=_public_checks(body.get("checks")))
                    return body
                last = f"status field {body.get('status') if isinstance(body, dict) else '?'}"
        else:
            last = f"http {status}"
        log.info("health not ready", detail=last)
        time.sleep(2)
    raise RuntimeError(f"health did not become ok: {last}")


def _public_checks(checks: Any) -> dict[str, Any]:
    """Reachability flags only, so a trace never grows a DSN or a key."""
    if not isinstance(checks, dict):
        return {}
    out: dict[str, Any] = {}
    for name, item in checks.items():
        if isinstance(item, dict):
            out[str(name)] = {
                "reachable": item.get("reachable"),
                "configured": item.get("configured"),
                "mode": item.get("mode"),
                "backend": item.get("backend"),
                "provider": item.get("provider"),
                "error": item.get("error"),
                "reason": item.get("reason"),
            }
    return out


def _pick_person(base: str, timeout: float) -> str:
    status, text = _request(f"{base}/personas", timeout=timeout)
    if status != 200:
        raise RuntimeError(f"GET /personas returned {status}")
    personas = json.loads(text)
    if not isinstance(personas, list) or not personas:
        raise RuntimeError("no personas configured")
    header = DemoHeaderIdentity.HEADER
    for persona in personas:
        pid = str(persona.get("id") or "")
        if not pid:
            continue
        code, _ = _request(
            f"{base}/sessions",
            method="POST",
            body={},
            headers={header: pid, "Content-Type": "application/json"},
            timeout=timeout,
        )
        if code == 200:
            log.info("smoke persona accepted", persona=pid)
            return pid
        log.info("smoke persona rejected", persona=pid, status=code)
    raise RuntimeError("no configured persona was accepted")


def _run_case(base: str, person: str, case: Case, cfg: SmokeConfig, timeout: float) -> list[str]:
    header = DemoHeaderIdentity.HEADER
    status, text = _request(
        f"{base}/chat",
        method="POST",
        body={"message": case.question},
        headers={header: person, "Content-Type": "application/json", "Accept": "text/event-stream"},
        timeout=timeout,
    )
    log.info("chat http", case=case.id, status=status, bytes=len(text))
    failures: list[str] = []
    if status != 200:
        return [f"{case.id}: HTTP {status}"]
    events = parse_sse(text)
    tools: list[str] = []
    tool_status: dict[str, str] = {}
    answer = ""
    final_status = ""
    errored = False
    for name, data in events:
        if name == "tool_start":
            tool = str(data.get("tool") or "")
            tools.append(tool)
            log.info("trace tool_start", case=case.id, tool=tool)
        elif name == "tool_end":
            tool = str(data.get("tool") or "")
            tool_status[tool] = str(data.get("status") or "")
            log.info("trace tool_end", case=case.id, tool=tool, tool_status=tool_status[tool])
        elif name == "final":
            answer = str(data.get("answer") or "")
            final_status = str(data.get("status") or "")
            log.info("trace final", case=case.id, final_status=final_status, answer=answer[:500])
        elif name == "error":
            errored = True
            log.info("trace error", case=case.id, error=data.get("error"), detail=str(data.get("message") or "")[:300])
        else:
            log.info("trace event", case=case.id, event=name)
    if errored:
        failures.append(f"{case.id}: error event")
    if final_status != case.expect.status:
        failures.append(f"{case.id}: final status {final_status or 'missing'} != {case.expect.status}")
    if case.expect.tools_any and not (set(tools) & set(case.expect.tools_any)):
        failures.append(f"{case.id}: tools {tools} missed {list(case.expect.tools_any)}")
    for wanted in case.expect.tools_any:
        if wanted in tool_status and tool_status[wanted] not in {"", "success"}:
            failures.append(f"{case.id}: {wanted} status {tool_status[wanted]}")
    if case.expect.abstains and not _abstained(answer, cfg):
        failures.append(f"{case.id}: answer did not abstain")
    if not case.expect.abstains and not answer.strip():
        failures.append(f"{case.id}: empty answer")
    return failures


def run_smoke(base: str, cases_path: Path, timeout: float) -> int:
    cfg = load_cases(cases_path)
    base = base.rstrip("/")
    try:
        _wait_health(base, timeout)
        person = _pick_person(base, timeout)
    except (RuntimeError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log.error("smoke setup failed: %s", exc)
        return 1
    failures: list[str] = []
    for case in cfg.cases:
        log.info("smoke case", case=case.id, question=case.question)
        try:
            failures.extend(_run_case(base, person, case, cfg, timeout))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failures.append(f"{case.id}: {type(exc).__name__}")
    if failures:
        for item in failures:
            log.error("smoke failed: %s", item)
        return 1
    log.info("smoke passed", cases=len(cfg.cases))
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging(get_settings())
    parser = argparse.ArgumentParser(prog="deploy_smoke")
    parser.add_argument("--base-url", default="", help="running app, e.g. http://127.0.0.1:7860")
    parser.add_argument("--cases", default="config/deploy_smoke.yaml")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--serve-fake", action="store_true", help="OpenAI-compatible stand-in for local smoke")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    cfg_path = Path(args.cases)
    if args.serve_fake:
        serve_fake(args.host, args.port, load_cases(cfg_path))
        return 0
    if not args.base_url:
        parser.error("--base-url is required unless --serve-fake")
    return run_smoke(args.base_url, cfg_path, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
