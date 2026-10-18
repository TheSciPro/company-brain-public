"""Load configured company sources into one FalkorDB graph per access level.

    PYTHONPATH=src:. .venv/bin/python scripts/load_company_graph.py --llm offline

``--llm auto`` uses the offline stand-in when ``OPENROUTER_API_KEY`` is unset, and the env model
(``LLM_GRAPH``) when it is set. The offline stand-in is not a live model. Live cloud verification
is ``scripts/verify_graph_live.py``. Counts are read from the graph after load.

The graph-name prefix is ``--prefix`` when that flag is passed, otherwise ``GRAPH_PREFIX`` /
``NWB_GRAPH_PREFIX`` (the same setting the app uses). Empty means no prefix.

Embeddings come from ``memory.yaml`` (the same embedder the app searches with). Naming a
different kind with ``--embedder`` is refused. The FalkorDB is ``FALKORDB_URL``, else
``falkor_target()``, else ``graph.yaml`` ``server.local_dir`` — the directory the app reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from graphrag_sdk.core.providers.base import LLMInterface

from nwb.actions.service import ActionService
from nwb.conf.loader import ConfigLoader
from nwb.conf.models import ActionsConfig, GraphConfig, MemoryConfig, ResolutionConfig
from nwb.config import get_settings, graph_name_prefix
from nwb.container import Container
from nwb.errors import ConfigError
from nwb.kg.load import load_company
from nwb.kg.models import GraphModels
from nwb.kg.offline_llm import OfflineSdkLLM
from nwb.kg.recording import CallRecorder, RecordedSdkLLM, RecordMode
from nwb.kg.resolution import ApprovalInbox
from nwb.kg.server import open_company_falkor, resolve_company_falkor
from nwb.llm.factory import LLMFactory
from nwb.llm.settings import LLMSettings
from nwb.memory.embeddings import Embedder, resolve_embedder
from nwb.obs.log import get_logger, new_trace, setup_logging

log = get_logger(__name__)


def main() -> None:
    setup_logging(get_settings())
    args = _parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    settings = get_settings()
    company = args.company or settings.company_id
    prefix = graph_name_prefix(args.prefix, settings.graph_prefix)
    if args.llm == "live" and not os.environ.get("OPENROUTER_API_KEY"):
        print("missing: OPENROUTER_API_KEY")
        raise SystemExit(2)
    with new_trace():
        try:
            report = asyncio.run(_run(root, args, company, prefix))
        except ConfigError as exc:
            print(str(exc))
            raise SystemExit(2) from None
    print(json.dumps(report, indent=1))


async def _run(root: Path, args: argparse.Namespace, company: str, prefix: str) -> dict[str, object]:
    conf = ConfigLoader(Path(args.config) if args.config else root / "config")
    cfg = conf.load("graph", GraphConfig)
    resolution = conf.load("resolution", ResolutionConfig)
    embedder = _embedder(conf, args.embedder)
    suggestions, reviewer = _inbox(conf, args.reviewer, resolution)
    mode = RecordMode(args.mode)
    cassette = Path(args.cassette_dir) if args.cassette_dir else root / "var" / "llm_calls" / "kg_load"
    recorder = CallRecorder(cassette, mode)
    llm, label, llm_phases = _choose_llm(cfg, recorder, args.llm)
    target = resolve_company_falkor(get_settings(), cfg.server, force_local=args.local)
    data = Path(args.data) if args.data else root / "data"
    max_rows = None if args.max_rows <= 0 else args.max_rows
    with tempfile.TemporaryDirectory() as tmp, open_company_falkor(target) as endpoint:
        log.info("falkordb endpoint", local=target.is_local, host=endpoint.host, port=endpoint.port)
        report = await load_company(
            cfg,
            data_dir=data,
            company=company,
            endpoint=endpoint,
            llm=llm,
            embedder=embedder,
            work_dir=Path(tmp) / "work",
            prefix=prefix,
            max_rows=max_rows,
            max_prose=args.max_prose,
            tables_only=args.tables_only,
            llm_phases=llm_phases,
            llm_label=label,
            resolution=resolution,
            suggestions=suggestions,
            reviewer=reviewer,
            asof=date.today(),
            ontology_path=root / cfg.company.ontology.file,
        )
    out = report.as_dict()
    out["recorded_hits"] = recorder.hits
    out["recorded_misses"] = recorder.misses
    out["live_verified"] = (not label.startswith("offline")) and mode is not RecordMode.REPLAY
    return out


def _inbox(conf: ConfigLoader, reviewer: str, resolution: ResolutionConfig) -> tuple[ApprovalInbox | None, str | None]:
    """File suggestions only when a reviewer id is given. Approving still does not merge."""
    if not reviewer:
        return None, None
    box = Container.from_settings(get_settings())
    actions = ActionService(box.db, conf.load("actions", ActionsConfig))
    sink = ApprovalInbox(actions, resolution.inbox.kind, resolution.inbox.proposed_by)
    return sink, reviewer


def _embedder(conf: ConfigLoader, kind: str | None) -> Embedder:
    return resolve_embedder(conf.load("memory", MemoryConfig).embedder, kind)


def _choose_llm(cfg: GraphConfig, recorder: CallRecorder, choice: str) -> tuple[LLMInterface, str, bool]:
    offline = choice == "offline" or (choice == "auto" and not os.environ.get("OPENROUTER_API_KEY"))
    if offline:
        model = cfg.company.load.offline_model if cfg.company.load else "offline-kg"
        return RecordedSdkLLM(OfflineSdkLLM(model), recorder), "offline", False
    models = GraphModels(LLMFactory(LLMSettings()), cfg, Path(__file__).resolve().parents[1])
    return models.sdk_llm(recorder), str(models.ref()), True


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Load one FalkorDB graph per access level.")
    ap.add_argument("--data", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--company", default="", help="tenant id; default is Settings.company_id")
    ap.add_argument(
        "--prefix",
        default=None,
        help="graph-name prefix; omit to use GRAPH_PREFIX. An empty value means no prefix",
    )
    ap.add_argument("--mode", default="record", choices=[m.value for m in RecordMode])
    ap.add_argument("--llm", default="auto", choices=["auto", "offline", "live"])
    ap.add_argument(
        "--embedder",
        default=None,
        choices=["config", "hashing", "fastembed"],
        help="memory.yaml embedder. Omit or pass config. A different kind is refused",
    )
    ap.add_argument("--max-prose", type=int, default=0, help="documents per level; 0 = all")
    ap.add_argument("--max-rows", type=int, default=0, help="rows per table; 0 = all")
    ap.add_argument("--tables-only", action="store_true")
    ap.add_argument(
        "--local",
        action="store_true",
        help="ignore FALKORDB_URL and use graph.yaml server.local_dir",
    )
    ap.add_argument(
        "--reviewer",
        default="",
        help="person id who receives probable-duplicate suggestions; omit to log them and not file",
    )
    ap.add_argument("--cassette-dir", default="")
    return ap


if __name__ == "__main__":
    main()
