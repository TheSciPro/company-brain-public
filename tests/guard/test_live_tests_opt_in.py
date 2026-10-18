"""A live test calls a hosted model with a real key. It must carry the ``live`` marker so it runs only on purpose."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.conftest import ROOT

TESTS = ROOT / "tests"


def _live_files() -> list[Path]:
    """Files named ``*_live.py`` or under ``tests/live/``."""
    return sorted(
        path for path in TESTS.rglob("test_*.py") if path.stem.endswith("_live") or path.parent.name == "live"
    )


def _marks_module_live(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
            return "pytest.mark.live" in ast.unparse(node.value)
    return False


def test_every_live_test_file_is_marked_live() -> None:
    files = _live_files()
    assert files, "expected the live test files to be found"
    unmarked = [str(path.relative_to(ROOT)) for path in files if not _marks_module_live(path)]
    assert unmarked == [], f"mark these with pytestmark = pytest.mark.live: {unmarked}"


def test_live_marker_is_registered() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"live:' in text
