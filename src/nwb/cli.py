"""Command line: ``python -m nwb <command>``."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nwb.agent.persistence import prepare_agent_storage
from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig
from nwb.config import Settings, get_settings
from nwb.container import Container, open_for_migrate
from nwb.db.copy_before_drop import CopyBeforeDrop
from nwb.db.migrate import Migrator
from nwb.errors import NwbError
from nwb.graph.schema import ensure_schema
from nwb.kg.tokenizer import TokenizerCache
from nwb.memory.sql_copy import SqlMemoriesToGraphiti, SqlSessionNotesToGraphiti
from nwb.obs.log import dataflow, get_logger, log_step, new_trace, setup_logging
from nwb.temporal.fact_copy import SqlFactsToGraph

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nwb")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    sub.add_parser("load-layer1", help="load org/people/groups/access from data/")
    sub.add_parser("load-layer2", help="load business records/documents + facts from data/")
    sub.add_parser("graph-sync", help="drain graph_outbox into FalkorDB")
    vis = sub.add_parser("visible", help="debug: record ids visible to a person as of a date")
    vis.add_argument("person")
    vis.add_argument("--asof", default=date.today().isoformat())
    chat = sub.add_parser("chat", help="interactive chat with the agent as a person (streams tokens)")
    chat.add_argument("--as", dest="person", required=True, help="person id (see demo/personas.yaml)")
    chat.add_argument("--asof", default=None, help="ask date (default: persona date from config, else today)")
    ask = sub.add_parser("ask", help="one-shot question(s) in a fresh session; ends the session after")
    ask.add_argument("--as", dest="person", required=True)
    ask.add_argument("--asof", default=None)
    ask.add_argument(
        "--on-interrupt",
        choices=["approve", "reject"],
        default="reject",
        help="answer to approval prompts (propose_action) in one-shot mode",
    )
    ask.add_argument("--keep", action="store_true", help="do not end the session (skip post-session notes)")
    ask.add_argument("question", nargs="+", help="one or more questions, asked in order in the same session")
    sub.add_parser("extract", help="LLM fact extraction for every document into the content-hash cache")
    ev = sub.add_parser("eval", help="agent evals: questions through the whole agent (replay cassettes or --live)")
    ev.add_argument("--live", action="store_true", help="call the LLM and re-record cassettes + transcripts")
    ev.add_argument("--case", default=None, help="run one case id")
    ev.add_argument("--cases", default="tests/fixtures/evals/cases.yaml")
    ev.add_argument("--cassettes", default="tests/fixtures/cassettes")
    ev.add_argument("--transcripts", default="docs/transcripts")
    demo = sub.add_parser("demo", help="ask the eval questions through the live agent and print transcripts")
    demo.add_argument("--only", default=None, help="run one case id")
    demo.add_argument("--cases", default="tests/fixtures/evals/cases.yaml")
    ob = sub.add_parser("onboarding", help="seed templates, create plans, print a brief")
    ob.add_argument("action", choices=["seed", "plans", "brief"])
    ob.add_argument("--as", dest="person", default=None)
    ob.add_argument("--asof", default=None)
    hb = sub.add_parser("heartbeat", help="run one heartbeat tick now (the scheduler runs it every N minutes)")
    hb.add_argument("--at", default=None, help="ISO datetime to pretend it is (tests active hours)")
    hb.add_argument("--asof", default=None, help="onboarding day to evaluate (persona date)")
    wa = sub.add_parser("watcher", help="run the watcher rules once: propose actions into the approval queue")
    wa.add_argument("--asof", default=None)
    ont = sub.add_parser("ontology", help="discover ontology changes and file them for approval")
    ont.add_argument("action", choices=["discover"])
    ont.add_argument("--asof", default=None)
    ont.add_argument("--force", action="store_true", help="re-run discovery even when sources are unchanged")
    ap = sub.add_parser("approvals", help="list approvals waiting on a person, or decide one")
    ap.add_argument("--as", dest="person", required=True)
    ap.add_argument("--decide", nargs=2, metavar=("APPROVAL_ID", "approve|reject"), default=None)
    ap.add_argument("--comment", default=None)
    sub.add_parser("post-session-sweep", help="run consolidation for ended sessions whose notes are still pending")
    sub.add_parser(
        "cache-tokenizers",
        help="download tiktoken encodings from config/graph.yaml into the local cache (offline chunking afterwards)",
    )
    serve = sub.add_parser("serve", help="run the FastAPI app (uvicorn)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--scheduler", action="store_true", help="also run heartbeat + post-session sweep jobs")
    args = parser.parse_args(argv)
    if args.cmd == "cache-tokenizers":
        return _cache_tokenizers()

    settings = get_settings()
    setup_logging(settings)
    with new_trace() as tid:
        dataflow("cli.start", cmd=args.cmd, app_env=settings.app_env, db=settings.db_backend, trace=tid)
        with log_step(f"cli.{args.cmd}", logger=log, level=logging.INFO):
            # Migrate before the app container: prod refuses to open DATABASE_URL unless it is already nw_app.
            if args.cmd == "migrate":
                try:
                    return _migrate(settings)
                except NwbError as exc:
                    log.warning("command stopped", cmd="migrate", error=type(exc).__name__, reason=str(exc))
                    print(f"error: {exc}")
                    return 1
            return _run(args, Container.from_settings(settings))


def _run(args: argparse.Namespace, c: Container) -> int:
    try:
        if args.cmd == "load-layer1":
            print(c.layer1_loader().load())
        elif args.cmd == "load-layer2":
            print(c.layer2_loader().load())
        elif args.cmd == "graph-sync":
            ensure_schema(c.graph)
            print(c.graph_sync().drain())
        elif args.cmd == "visible":
            print(c.access_policy().visible_record_ids(args.person, date.fromisoformat(args.asof)))
        elif args.cmd in {"chat", "ask"}:
            from nwb.chat_cli import ChatCli

            fixed = getattr(args, "on_interrupt", None)
            cli = ChatCli(c, args.person, args.asof, decide=(lambda _req: fixed) if fixed else None)
            return cli.repl() if args.cmd == "chat" else cli.ask(args.question, end=not args.keep)
        elif args.cmd == "demo":
            from nwb.demo import run_demo

            return run_demo(c, Path(args.cases), only=args.only)
        elif args.cmd == "extract":
            return _extract(c)
        elif args.cmd == "eval":
            return _eval(c, args)
        elif args.cmd == "onboarding":
            return _onboarding(c, args.action, args.person, args.asof)
        elif args.cmd == "heartbeat":
            at = datetime.fromisoformat(args.at) if args.at else datetime.now(ZoneInfo(c.agent_config.timezone))
            print(c.heartbeat.tick(at, date.fromisoformat(args.asof) if args.asof else None))
        elif args.cmd == "watcher":
            print(c.watcher.run(date.fromisoformat(args.asof) if args.asof else date.today()).summary())
        elif args.cmd == "ontology":
            return _ontology(c, args.asof, args.force)
        elif args.cmd == "approvals":
            return _approvals(c, args.person, args.decide, args.comment)
        elif args.cmd == "post-session-sweep":
            for rep in c.consolidation.sweep():
                print(rep)
        elif args.cmd == "serve":
            import uvicorn

            if args.scheduler:
                from nwb.jobs.scheduler import build_scheduler

                build_scheduler(c).start()
            # log_config=None: uvicorn logs through our handlers (one format, one trace id) instead of its own.
            uvicorn.run("nwb.api.app:create_app", factory=True, host=args.host, port=args.port, log_config=None)
    except NwbError as exc:  # user-facing errors (not found, denied, conflict): one line, non-zero exit
        log.warning("command stopped", cmd=args.cmd, error=type(exc).__name__, reason=str(exc))
        print(f"error: {exc}")
        return 1
    finally:
        c.close()
    return 0


def _cache_tokenizers() -> int:
    """Install-time step. Does not open the database: a Docker build only needs the encoding files."""
    settings = get_settings()
    setup_logging(settings)
    try:
        cfg = ConfigLoader(settings.config_dir).load("graph", GraphConfig)
        directory = TokenizerCache.from_config(Path.cwd(), cfg).prepare()
    except NwbError as exc:
        log.warning("command stopped", cmd="cache-tokenizers", error=type(exc).__name__, reason=str(exc))
        print(f"error: {exc}")
        return 1
    print(f"tiktoken cache: {directory} ({', '.join(cfg.tokenizer.encodings)})")
    return 0


def _migrate(settings: Settings) -> int:
    db = open_for_migrate(settings)
    c = Container(settings=settings, db=db)
    copies = CopyBeforeDrop(
        db,
        {
            "0018_drop_sql_memory": (
                lambda: SqlMemoriesToGraphiti(lambda: c.personal_memory),
                lambda: SqlSessionNotesToGraphiti(lambda: c.personal_memory),
            ),
            "0019_drop_facts": (lambda: SqlFactsToGraph(lambda: c.fact_book, c.classification.unknown_tag.level),),
        },
    )
    try:
        ran = Migrator(db, settings.migrations_dir, before=copies).migrate()
        if settings.db_backend == "postgres":
            prepare_agent_storage(settings.migrate_url or settings.pg_dsn)
            log.info("agent storage ready")
        print(f"applied {len(ran)} migration(s): {', '.join(ran) or '-'}")
    finally:
        c.close()
    return 0


def _ontology(c: Container, asof_s: str | None, force: bool) -> int:
    asof = date.fromisoformat(asof_s) if asof_s else date.today()
    report = asyncio.run(
        c.ontology.refresh(data_dir=c.settings.data_dir, llm=c.graph_sdk_llm(), asof=asof, unattended=True, force=force)
    )
    print(report.as_dict())
    return 0


def _approvals(c: Container, person: str, decide: list[str] | None, comment: str | None) -> int:
    if decide:
        approval_id, decision = decide
        if decision not in ("approve", "reject"):
            print("decision must be approve or reject")
            return 2
        print(
            c.actions.decide(
                person, approval_id, "approve" if decision == "approve" else "reject", comment
            ).model_dump_json(indent=2)
        )
        return 0
    pending = c.actions.pending_for(person)
    for a in pending:
        print(f"{a.approval_id}  {a.kind:<20} {a.title}  (requested by {a.requester}, via {a.proposed_by})")
    print(f"{len(pending)} pending")
    return 0


def _onboarding(c: Container, action: str, person: str | None, asof_s: str | None) -> int:
    asof = date.fromisoformat(asof_s) if asof_s else date.today()
    if action == "seed":
        print(f"seeded {c.maintenance_onboarding.seed_templates()} onboarding templates")
    elif action == "plans":
        made = c.maintenance_onboarding.ensure_plans(asof)
        print(f"created {len(made)} plan(s) for hires in the window: {', '.join(made) or '-'}")
    else:
        if not person:
            print("--as is required for brief")
            return 2
        print(c.onboarding.brief(person, asof).model_dump_json(indent=2))
    return 0


def _extract(c: Container) -> int:
    """Re-run layer 2 with live extraction (``NWB_EXTRACTION_MODE=llm``); unchanged documents hit the cache."""
    if c.settings.extraction_mode != "llm":
        print("set NWB_EXTRACTION_MODE=llm to call the model; cache-only mode only reads the cache")
    rep = c.layer2_loader().load()
    print(rep)
    return 0


def _eval(c: Container, args: argparse.Namespace) -> int:
    from nwb.evals import EvalRunner, load_cases

    runner = EvalRunner(c)
    cases = [k for k in load_cases(Path(args.cases)) if args.case in (None, k.id)]
    failed = 0
    for case in cases:
        if args.live:
            res = runner.run_live(case, Path(args.cassettes), Path(args.transcripts))
        else:
            res = runner.run_replay(case, Path(args.cassettes) / f"{case.id}.json")
        failed += not res.passed
        print(res.explain(), end="\n\n")
    print(f"{len(cases) - failed}/{len(cases)} eval cases pass")
    return 1 if failed else 0
