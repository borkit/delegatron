"""Guard: workspace files are never decoded with the host locale (#117).

On Windows, ``read_text()`` / ``write_text()`` / ``open()`` without
``encoding=`` use the ANSI code page (usually cp1252), so the model sees
mojibake and UTF-8 manifests fail to decode.  Every text-mode call in these
modules must name its encoding; binary mode is fine.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import delegatron

_SRC = Path(delegatron.__file__).parent

_MODULES = [
    "context.py",
    "repomap.py",
    "detection.py",
    "framework_hints.py",
    "workspace.py",
    "security/import_validator.py",
    "security/patch_lines.py",
    "server/run_workspace.py",
    *sorted(path.relative_to(_SRC).as_posix() for path in (_SRC / "sandbox").glob("*.py")),
]


def _text_io_calls_without_encoding(source: str) -> list[int]:
    """Line numbers of read_text/write_text/open calls in text mode without ``encoding=``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"read_text", "write_text", "open"}:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        modes = [
            arg.value
            for arg in [*node.args, *(kw.value for kw in node.keywords if kw.arg == "mode")]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if name == "open" and any("b" in mode for mode in modes):
            continue
        lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("module", _MODULES)
def test_text_io_names_its_encoding(module: str) -> None:
    source = (_SRC / module).read_text(encoding="utf-8")

    assert _text_io_calls_without_encoding(source) == []


def test_guard_flags_locale_dependent_calls() -> None:
    source = (
        "p.read_text()\n"
        "p.write_text(s)\n"
        "open(f)\n"
        "p.open('rb')\n"
        "p.read_text(encoding='utf-8')\n"
        "open(f, mode='rb')\n"
    )

    assert _text_io_calls_without_encoding(source) == [1, 2, 3]
