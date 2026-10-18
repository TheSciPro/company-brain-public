"""One-command live check, for when cloud secrets exist. Not run by tests.

    PYTHONPATH=src:. .venv/bin/python scripts/verify_graph_live.py

Requires ``OPENROUTER_API_KEY`` and ``FALKORDB_URL`` (redis://user:pass@host:port). If either is
missing the process exits 2 and prints only the missing names. It sets ``LLM_GRAPH`` to the free
Nemotron model when that variable is unset, records calls under ``var/llm_calls/kg_live``, and
uses a fresh ``ca_<branch>_`` graph-name prefix so a shared FalkorDB is not overwritten.

Extra arguments are forwarded to ``scripts/load_company_graph.py`` (for example ``--max-prose 0``).

The model is the env model, not the offline stand-in. Embeddings come from ``memory.yaml``,
the same embedder the app searches with. A mismatched ``--embedder`` is refused.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REQUIRED = ("OPENROUTER_API_KEY", "FALKORDB_URL")
_DEFAULT_MODEL = "openrouter:nvidia/nemotron-3-super-120b-a12b:free"


def main() -> None:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        print("missing: " + ", ".join(missing))
        raise SystemExit(2)
    os.environ.setdefault("LLM_GRAPH", _DEFAULT_MODEL)
    root = Path(__file__).resolve().parents[1]
    extra = sys.argv[1:]
    sys.argv = [
        "load_company_graph.py",
        "--llm",
        "live",
        "--mode",
        "record",
        "--prefix",
        _prefix(root),
        "--max-prose",
        "6",
        "--cassette-dir",
        str(root / "var" / "llm_calls" / "kg_live"),
        *extra,
    ]
    _load_main(root / "scripts" / "load_company_graph.py")()


def _prefix(root: Path) -> str:
    branch = "detached"
    head = root / ".git" / "HEAD"
    if head.is_file():
        text = head.read_text(encoding="utf-8").strip()
        marker = "ref: refs/heads/"
        if text.startswith(marker):
            branch = text.removeprefix(marker)
    safe = "".join(ch.lower() if ch.isalnum() or ch in "_-" else "_" for ch in branch)
    return f"ca_{safe}_"


def _load_main(path: Path) -> object:
    spec = importlib.util.spec_from_file_location("load_company_graph", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


if __name__ == "__main__":
    main()
