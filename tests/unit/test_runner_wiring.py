"""graph.Pipeline is the one runner: nothing but the package's re-export imports the
deprecated ``AsyncPipeline`` module (#83).  A static AST scan, no imports executed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import delegatron

ASYNC_RUNNER = "delegatron.orchestrator.async_runner"
PACKAGE_ROOT = Path(delegatron.__file__).resolve().parent
# Kept for one release so ``from delegatron.orchestrator import AsyncPipeline`` still works.
ALLOWED = {"orchestrator/__init__.py"}


def _module_name(path: Path) -> str:
    """``delegatron.x.y`` for a source file of the package (``__init__`` is its package)."""
    parts = ["delegatron", *path.relative_to(PACKAGE_ROOT).with_suffix("").parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported_modules(path: Path) -> set[str]:
    """Modules *path* imports, relative imports resolved against its package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _module_name(path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{anchor}.{base}" if base else anchor
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_only_the_package_init_imports_the_async_runner() -> None:
    """The re-export is found too, so the scan does see such imports."""
    importers = {
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if ASYNC_RUNNER in _imported_modules(path)
    }

    assert importers == ALLOWED
