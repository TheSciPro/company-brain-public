"""FastAPI application factory (no ``from __future__ import annotations``: FastAPI resolves local Depends aliases).

Routes are thin: they resolve identity, call the ChatRunner / services, and serialise ``ChatEvent`` as SSE
(``event: <type>``, ``data: <json>``). Errors map from domain exceptions to HTTP codes in one place. Every request
shares one trace id from ``nwb.obs.log`` with the agent. A chat logs ``response out`` when the stream finishes.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import asdict
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from nwb.actions.models import ActionView
from nwb.agent.clock import resolve_asof
from nwb.agent.events import ChatEvent
from nwb.api.health import collect_checks, health_payload
from nwb.api.identity import DemoHeaderIdentity, IdentityProvider
from nwb.api.schemas import (
    ChatRequest,
    DecideRequest,
    InboxItemOut,
    PersonaOut,
    ResumeRequest,
    SessionResponse,
    StartSessionRequest,
)
from nwb.config import Settings, get_settings
from nwb.container import Container
from nwb.db.repo_onboarding import InboxRepository
from nwb.errors import AccessDeniedError, ConflictError, NotFoundError
from nwb.obs.log import Hop, dataflow, get_logger, new_trace, setup_logging, trace_id
from nwb.onboarding.models import OnboardingBrief

log = get_logger(__name__)


async def _sse(
    make_events: Callable[[], Iterator[ChatEvent]],
    *,
    trace: str,
    started: float,
    method: str,
    path: str,
) -> AsyncIterator[dict[str, str]]:
    """Run the (sync) agent stream on ONE worker thread and relay events to the event loop.

    The worker re-binds the request's trace id (context vars do not cross threads). ``response out`` is logged
    here, after the stream finishes, with the run status and the duration since the request started.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[ChatEvent | None] = asyncio.Queue()

    def worker() -> None:
        with new_trace(trace):
            try:
                for ev in make_events():
                    loop.call_soon_threadsafe(queue.put_nowait, ev)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

    done = loop.run_in_executor(None, worker)
    status = "failed"
    try:
        while (ev := await queue.get()) is not None:
            if ev.type == "final":
                status = str(ev.data.get("status") or "done")
            elif ev.type == "error":
                status = "failed"
            yield {"event": ev.type, "data": json.dumps({"run_id": ev.run_id, **ev.data}, default=str)}
    finally:
        try:
            await done
        finally:
            ms = int((time.perf_counter() - started) * 1000)
            with new_trace(trace):
                log.info("response out", method=method, path=path, status=status, ms=ms)


def create_app(container: Container | None = None, identity: IdentityProvider | None = None) -> FastAPI:
    """API entry point. Without an injected container (uvicorn factory / ``nwb serve``) it sets up logging itself;
    tests inject a container and keep their own logging."""
    settings: Settings = container.settings if container else get_settings()
    if container is None:
        setup_logging(settings)
    c = container or Container.from_settings(settings)
    ident = identity or DemoHeaderIdentity(c.db)
    app = FastAPI(title="Company Brain", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.api_cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", DemoHeaderIdentity.HEADER],
    )

    @app.middleware("http")
    async def _request_log(request: Request, call_next: Callable[[Request], Any]) -> Response:
        """One trace id per request, echoed as ``X-Trace-Id``.

        A streaming chat logs ``response out`` when the body finishes (``_sse``), so the duration covers the
        agent run. Every other route logs it here, when the handler returns.
        """
        with new_trace(request.headers.get("x-trace-id") or None) as tid:
            request.state.nwb_started = time.perf_counter()
            log.info("request in", method=request.method, path=request.url.path)
            try:
                response: Response = await call_next(request)
            except Exception:
                log.exception("request crashed", method=request.method, path=request.url.path)
                raise
            response.headers["X-Trace-Id"] = tid
            # BaseHTTPMiddleware replaces the handler's response with its own streaming wrapper, so the
            # chat flag (set on the shared request state) is what tells us to wait for the stream.
            if not getattr(request.state, "nwb_stream", False):
                ms = int((time.perf_counter() - request.state.nwb_started) * 1000)
                log.info(
                    "response out",
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    ms=ms,
                )
            return response

    def _stream(request: Request, make: Callable[[], Iterator[ChatEvent]]) -> EventSourceResponse:
        """Hand the middleware's trace id to the worker. Captured before the middleware exits."""
        request.state.nwb_stream = True
        return EventSourceResponse(
            _sse(
                make,
                trace=trace_id(),
                started=float(getattr(request.state, "nwb_started", time.perf_counter())),
                method=request.method,
                path=request.url.path,
            )
        )

    def person(request: Request) -> str:
        pid = ident.person_id(request)
        log.debug("identity resolved", person=pid, via=type(ident).__name__)
        return pid

    Person = Annotated[str, Depends(person)]  # noqa: N806 - a type alias, read as one

    def _asof(person_id: str, explicit: date | None) -> date:
        persona = c.personas.get(person_id)
        return resolve_asof(explicit, persona.asof if persona else c.agent_config.default_asof, c.clock)

    @app.exception_handler(NotFoundError)
    def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        log.info("not found", path=request.url.path, reason=str(exc))
        return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)

    @app.exception_handler(AccessDeniedError)
    def _denied(request: Request, exc: AccessDeniedError) -> JSONResponse:
        log.warning("access denied", path=request.url.path, reason=str(exc))
        return JSONResponse({"error": "forbidden", "message": str(exc)}, status_code=403)

    @app.exception_handler(ConflictError)
    def _conflict(request: Request, exc: ConflictError) -> JSONResponse:
        log.info("conflict", path=request.url.path, reason=str(exc))
        return JSONResponse({"error": "conflict", "message": str(exc)}, status_code=409)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Process is up. FalkorDB, Postgres and the LLM are reported separately and carry no secrets."""

        def falkor_query() -> list[list[Any]]:
            return c.graph.ro_query("RETURN 1")

        def postgres_ok() -> Any:
            with c.db.unit_of_work() as uow:
                return uow.fetchone("SELECT 1 AS ok")

        return health_payload(settings, collect_checks(settings, falkor_query=falkor_query, postgres_ok=postgres_ok))

    @app.get("/personas")
    def personas() -> list[PersonaOut]:
        return [PersonaOut(id=p.id, label=p.label, asof=p.asof) for p in c.personas.personas]

    @app.post("/sessions")
    def start_session(body: StartSessionRequest, person_id: Person) -> SessionResponse:
        asof = _asof(person_id, body.asof)
        sid = c.runner.start_session(person_id, asof)
        return SessionResponse(session_id=sid, person_id=person_id, asof=asof)

    @app.post("/sessions/{session_id}/end")
    def end_session(session_id: str, person_id: Person) -> dict[str, str]:
        c.runner.end_session(person_id, session_id)
        return {"session_id": session_id, "status": "ended"}

    @app.post("/chat")
    def chat(body: ChatRequest, person_id: Person, request: Request) -> EventSourceResponse:
        sid = body.session_id or c.runner.start_session(person_id, _asof(person_id, body.asof))
        dataflow(Hop.API, route="/chat", person=person_id, session=sid)
        return _stream(request, lambda: c.runner.stream(person_id, sid, body.message))

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str, body: ResumeRequest, person_id: Person, request: Request) -> EventSourceResponse:
        dataflow(Hop.API, route="/runs/resume", person=person_id, run=run_id)
        return _stream(request, lambda: c.runner.resume(person_id, run_id, body.decisions))

    @app.get("/onboarding/brief")
    def onboarding_brief(person_id: Person, asof: date | None = None) -> OnboardingBrief:
        return c.onboarding.brief(person_id, _asof(person_id, asof))

    @app.get("/inbox")
    def inbox(person_id: Person) -> list[InboxItemOut]:
        with c.db.unit_of_work(as_person=person_id) as uow:
            rows = InboxRepository(uow).list(person_id)
        return [InboxItemOut(**asdict(r)) for r in rows]

    @app.post("/inbox/{msg_id}/read")
    def inbox_read(msg_id: str, person_id: Person) -> dict[str, str]:
        with c.db.unit_of_work(as_person=person_id) as uow:
            InboxRepository(uow).mark_read(person_id, msg_id)
        return {"id": msg_id, "status": "read"}

    @app.get("/approvals")
    def approvals(person_id: Person) -> list[ActionView]:
        return c.actions.pending_for(person_id)

    @app.post("/approvals/{approval_id}/decide")
    def decide(approval_id: str, body: DecideRequest, person_id: Person) -> ActionView:
        dataflow(Hop.API, route="/approvals/decide", person=person_id, approval=approval_id, decision=body.decision)
        return c.actions.decide(person_id, approval_id, body.decision, body.comment)

    app.state.container = c
    return app
