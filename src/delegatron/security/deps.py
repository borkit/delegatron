"""Dependency auditing — detect new third-party imports introduced by patches.

Extracts Python import statements from the lines a patch adds, classifies
each imported module as stdlib / declared / local / new, and flags new
dependencies for human review.  Declared dependencies and local modules are
resolved by the import validator against the patch overlaid on the
workspace, so a ``requirements.txt`` or module added by the same patch
counts.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from delegatron.security.import_validator import (
    _PY_SANDBOX_PROVIDED,
    _py_declared_deps,
    _py_is_local,
    _py_normalize,
)
from delegatron.security.patch_lines import AddedLines, added_lines, patch_files

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Known Python 3.11+ stdlib top-level modules (subset covering common usage).
# Comprehensive enough for practical auditing; exotic C-extension modules that
# are almost never imported directly (e.g. _thread) are omitted intentionally.
# --------------------------------------------------------------------------- #
_STDLIB_MODULES: frozenset[str] = frozenset(
    sorted(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else []
) | frozenset(
    {
        # Guarantee a baseline even on older interpreters
        "abc",
        "argparse",
        "ast",
        "asyncio",
        "base64",
        "collections",
        "configparser",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fnmatch",
        "functools",
        "glob",
        "gzip",
        "hashlib",
        "html",
        "http",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "multiprocessing",
        "operator",
        "os",
        "pathlib",
        "pickle",
        "platform",
        "pprint",
        "queue",
        "random",
        "re",
        "shutil",
        "signal",
        "socket",
        "sqlite3",
        "string",
        "struct",
        "subprocess",
        "sys",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "typing",
        "unittest",
        "urllib",
        "uuid",
        "warnings",
        "xml",
        "zipfile",
    }
)

# Regex patterns for import statements
# ``import a.b as c, d`` imports every listed module; the list is group 1.
_IMPORTED_NAME = r"[\w.]+(?:\s+as\s+\w+)?"
_IMPORT_RE = re.compile(rf"^\s*import\s+({_IMPORTED_NAME}(?:\s*,\s*{_IMPORTED_NAME})*)")
_FROM_IMPORT_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+")


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class DepFinding:
    """A single dependency finding from patch audit."""

    package_name: str
    import_line: str
    severity: str  # "info" | "warning"
    file: str = ""
    line_number: int = 0  # 1-based line in the file after the patch is applied


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _extract_imports(lines: list[str]) -> list[tuple[str, str]]:
    """Extract (top_level_module, original_line) pairs for absolute imports.

    Relative imports (``from . import x``) are project-internal and skipped.
    """
    results: list[tuple[str, str]] = []
    for line in lines:
        match = _IMPORT_RE.match(line)
        if match:
            # "import os, numpy as np" -> os, numpy
            modules = [name.split()[0] for name in match.group(1).split(",")]
        else:
            match = _FROM_IMPORT_RE.match(line)
            modules = [match.group(1)] if match else []
        for module in modules:
            if not module.startswith("."):
                results.append((module.split(".")[0], line.strip()))
    return results


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def audit_imports(
    patch: Mapping[str, Any],
    project_root: Path,
    *,
    added: AddedLines | None = None,
) -> list[DepFinding]:
    """Audit a patch for new third-party dependencies.

    Args:
        patch: A PatchSet dict with ``files`` and/or ``diff_unified``.
        project_root: Path to the project root (where pyproject.toml etc. live).
        added: Precomputed :func:`~delegatron.security.patch_lines.added_lines`;
            computed against *project_root* when omitted.

    Returns:
        One :class:`DepFinding` per newly introduced top-level module, at the
        first added line that imports it.
    """
    if added is None:
        added = added_lines(patch, project_root)
    py_lines = {path: entries for path, entries in added.items() if path.endswith(".py")}
    if not py_lines:
        return []

    files = patch_files(patch)
    declared = _py_declared_deps(project_root, files)
    patch_py = frozenset(path for path in files if path.endswith(".py"))

    findings: list[DepFinding] = []
    seen: set[str] = set()
    for path, entries in py_lines.items():
        importer_dir = PurePosixPath(path).parent
        for lineno, text in entries:
            for module, statement in _extract_imports([text]):
                if module in seen:
                    continue
                seen.add(module)
                if module in _STDLIB_MODULES or module in _PY_SANDBOX_PROVIDED:
                    continue
                if _py_normalize(module) in declared:
                    continue
                if _py_is_local(module, importer_dir, project_root, patch_py):
                    continue
                findings.append(
                    DepFinding(
                        package_name=module,
                        import_line=statement,
                        severity="warning",
                        file=path,
                        line_number=lineno,
                    )
                )
                logger.info("New dependency detected: %s (from: %s)", module, statement)

    return findings
