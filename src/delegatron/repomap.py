"""Repository map — structural overview of the codebase for LLM context.

Inspired by Aider's repo map approach: parse source files to extract
class/function definitions, build a dependency graph, rank by relevance
to the current task using PageRank, and produce a compact structural
skeleton that gives the LLM architectural awareness.

Uses two parsing backends:
1. Python's built-in ``ast`` module for .py files (richer signatures)
2. ``tree-sitter-language-pack`` for all other languages (305 supported)

The map is complementary to ``context.py``'s full-file content:
- Repo map = "where am I" (structural skeleton of the whole project)
- Context = "what to read" (full content of relevant files)
"""

from __future__ import annotations

import ast
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from delegatron.utils.paths import internal_dirs_for

logger = logging.getLogger(__name__)

# Max token budget for the repo map (approximate, 1 token ≈ 4 chars)
DEFAULT_MAP_TOKENS = 1024
CHARS_PER_TOKEN = 4

# File extensions we can parse with the built-in Python AST
_PYTHON_EXTS = {".py"}

# Skip these directories at any depth.  Hidden directories (``.git``,
# ``.venv``, ``.delegatron-runs``, ...) are skipped too, and Delegatron's
# own directories at the workspace root (``internal_dirs_for``).
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", "dist", "build", "venv"})

# Skip files larger than this
_MAX_FILE_SIZE = 100_000

# Symbols listed per file; the rest are summarised as "... +N more"
_MAX_SYMBOLS_PER_FILE = 25

# Stop filling the map once fewer characters than this are left
_MIN_MAP_ENTRY_CHARS = 40


@dataclass
class Symbol:
    """A code symbol (class, function, method) with its location."""

    name: str
    kind: str  # "class", "function", "method"
    file: str  # relative path
    line: int
    signature: str = ""  # e.g., "def foo(a: int, b: str) -> bool"
    parent: str = ""  # parent class name for methods


@dataclass
class FileSymbols:
    """All symbols extracted from a single file."""

    path: str  # relative path
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # imported module names


def generate_repo_map(
    workspace_root: Path,
    task_context: str = "",
    mentioned_files: list[str] | None = None,
    max_tokens: int = DEFAULT_MAP_TOKENS,
    languages: list[str] | None = None,
) -> str:
    """Generate a structural map of the repository.

    Args:
        workspace_root: Root directory of the project.
        task_context: The task description (used to boost relevant files).
        mentioned_files: Files explicitly mentioned in task/review context.
        max_tokens: Approximate token budget for the map.
        languages: Detected project languages (from detection.py).
            Advisory only: files are picked by extension, so an empty list
            (no marker files detected) still maps every parseable file.

    Returns:
        A formatted string showing the project structure with
        class/function signatures, suitable for LLM context.
    """
    max_chars = max_tokens * CHARS_PER_TOKEN

    # Collect all parseable files
    all_files = _collect_files(workspace_root)

    # Extract symbols from each file; one file that fails must not empty the map
    file_symbols: list[FileSymbols] = []
    for rel_path in all_files:
        try:
            syms = _extract_symbols(workspace_root / rel_path, rel_path)
        except Exception as e:
            logger.debug("Repo map: skipping %s: %s", rel_path, e)
            continue
        if syms and syms.symbols:
            file_symbols.append(syms)

    if not file_symbols:
        return ""

    # Rank files by relevance to the task
    ranked = _rank_files(file_symbols, task_context, mentioned_files or [])

    # Format the map within the token budget
    return _format_map(ranked, max_chars)


def list_workspace_files(workspace_root: Path) -> list[str]:
    """List every file in the workspace as sorted ``/``-separated relative paths.

    Skips hidden directories (``.git``, ``.venv``, ``.delegatron-runs``, ...),
    the dependency, cache and build directories in ``_SKIP_DIRS``, and
    Delegatron's own directories (``_sandbox/``, its ``runs/`` and
    ``reviews/``: ``internal_dirs_for``) at the root only, so a nested
    ``src/runs/`` package, or a root ``runs/`` package of the user's, is
    still listed.
    """
    root = workspace_root.resolve()
    root_skip = _SKIP_DIRS.union(internal_dirs_for(root))
    files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        skip = root_skip if rel_dir == Path() else _SKIP_DIRS
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        files.extend((rel_dir / fname).as_posix() for fname in filenames)

    return sorted(files)


def _collect_files(workspace_root: Path) -> list[str]:
    """Collect all parseable source files in the workspace (posix paths)."""
    # Include Python + all tree-sitter supported extensions
    exts = tuple(_PYTHON_EXTS | _TREE_SITTER_EXTS)
    root = workspace_root.resolve()

    files: list[str] = []
    for rel in list_workspace_files(root):
        if not rel.endswith(exts):
            continue
        try:
            if (root / rel).stat().st_size > _MAX_FILE_SIZE:
                continue
        except OSError as e:
            logger.debug("Repo map: skipping %s: %s", rel, e)
            continue
        files.append(rel)

    return files


def _extract_symbols(full_path: Path, rel_path: str) -> FileSymbols | None:
    """Extract symbols from a source file.

    Uses Python's ast module for .py files (richer signatures with
    type annotations). Uses tree-sitter for all other languages,
    with a regex fallback if tree-sitter parsing fails.
    """
    if full_path.suffix in _PYTHON_EXTS:
        return _extract_python_symbols(full_path, rel_path)

    result = _extract_tree_sitter_symbols(full_path, rel_path)
    if result and result.symbols:
        return result

    # Regex fallback for edge cases where tree-sitter fails
    return _extract_basic_symbols(full_path, rel_path)


def _extract_python_symbols(full_path: Path, rel_path: str) -> FileSymbols | None:
    """Extract symbols from a Python file using the ast module."""
    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, OSError):
        return None

    result = FileSymbols(path=rel_path)

    # Extract imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.imports.append(node.module)

    # Extract top-level classes and functions
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            sig = f"class {node.name}"
            # Add base classes
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(ast.unparse(base))
            if bases:
                sig += f"({', '.join(bases)})"

            result.symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    file=rel_path,
                    line=node.lineno,
                    signature=sig,
                )
            )

            # Extract methods
            for item in ast.iter_child_nodes(node):
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    msig = _format_func_signature(item)
                    result.symbols.append(
                        Symbol(
                            name=item.name,
                            kind="method",
                            file=rel_path,
                            line=item.lineno,
                            signature=msig,
                            parent=node.name,
                        )
                    )

        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            sig = _format_func_signature(node)
            result.symbols.append(
                Symbol(
                    name=node.name,
                    kind="function",
                    file=rel_path,
                    line=node.lineno,
                    signature=sig,
                )
            )

    return result


def _format_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format a function/method signature from an AST node."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    import contextlib

    args_parts: list[str] = []

    for arg in node.args.args:
        name = arg.arg
        if arg.annotation:
            with contextlib.suppress(Exception):
                name += f": {ast.unparse(arg.annotation)}"
        args_parts.append(name)

    ret = ""
    if node.returns:
        with contextlib.suppress(Exception):
            ret = f" -> {ast.unparse(node.returns)}"

    return f"{prefix} {node.name}({', '.join(args_parts)}){ret}"


def _extract_basic_symbols(full_path: Path, rel_path: str) -> FileSymbols | None:
    """Basic symbol extraction using simple patterns (no ast, no tree-sitter).

    Catches common patterns like 'class Foo', 'def bar()', 'function baz()',
    'export function', etc. Not as accurate as AST parsing but works for
    any language.
    """
    import re

    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    result = FileSymbols(path=rel_path)

    patterns = [
        (r"^\s*(?:export\s+)?class\s+(\w+)", "class"),
        (r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function)\s+(\w+)\s*\(", "function"),
        (r"^\s*(?:pub\s+)?fn\s+(\w+)\s*[<(]", "function"),  # Rust
        # Java/C#
        (r"^\s*(?:public|private|protected)?\s*(?:static\s+)?\w+\s+(\w+)\s*\(", "method"),
        (r"^\s*func\s+(\w+)\s*\(", "function"),  # Go
    ]

    for i, line in enumerate(source.split("\n"), 1):
        for pattern, kind in patterns:
            m = re.match(pattern, line)
            if m:
                name = m.group(1)
                # Skip test/private helpers for brevity
                if name.startswith("_") and name != "__init__":
                    continue
                result.symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        file=rel_path,
                        line=i,
                        signature=line.strip()[:100],
                    )
                )
                break

    return result if result.symbols else None


def _rank_files(
    file_symbols: list[FileSymbols],
    task_context: str,
    mentioned_files: list[str],
) -> list[FileSymbols]:
    """Rank files by relevance to the task.

    Uses a simple scoring heuristic inspired by Aider's PageRank approach:
    - Files mentioned in the task get a high boost
    - Files with symbols mentioned in the task get a medium boost
    - Otherwise ranked by number of definitions (more = more important),
      on a log scale so file size cannot outweigh relevance
    """
    task_lower = task_context.lower()
    mentioned_set = set(mentioned_files)

    scores: list[tuple[float, FileSymbols]] = []
    for fs in file_symbols:
        score = 0.0

        # Direct mention in task or mentioned files
        if fs.path in mentioned_set:
            score += 100.0
        elif any(part in task_lower for part in fs.path.split("/")):
            score += 50.0

        # Symbol names mentioned in task
        for sym in fs.symbols:
            if sym.name.lower() in task_lower:
                score += 20.0
            # Partial match for compound names (e.g., "create_user" matches "user")
            elif any(
                part in task_lower
                for part in sym.name.lower().replace("_", " ").split()
                if len(part) >= 4
            ):
                score += 5.0

        # Base score from number of definitions
        score += 2.0 * math.log1p(len(fs.symbols))

        # Strongly deprioritize test files — they clutter the map
        # and the implementer needs to see source structure, not tests
        if "test" in fs.path.lower() or "tests/" in fs.path:
            score *= 0.3
        else:
            score += 5.0

        scores.append((score, fs))

    # Sort by score descending
    scores.sort(key=lambda x: x[0], reverse=True)
    return [fs for _, fs in scores]


def _format_map(ranked: list[FileSymbols], max_chars: int) -> str:
    """Format the ranked files into a compact structural map of at most *max_chars*.

    Each file lists up to ``_MAX_SYMBOLS_PER_FILE`` symbols.  A file whose
    block does not fit what is left is shown as ``path: (N symbols)``, or
    skipped when even that does not fit; later, smaller files are still tried.
    """
    blocks: list[str] = []
    used = 0

    for fs in ranked:
        separator = 1 if blocks else 0  # blocks are joined by a blank line
        room = max_chars - used - separator
        if room < _MIN_MAP_ENTRY_CHARS:
            break
        for text in (_format_file_block(fs), f"{fs.path}: ({len(fs.symbols)} symbols)\n"):
            if len(text) <= room:
                blocks.append(text)
                used += separator + len(text)
                break

    return "\n".join(blocks)


def _format_file_block(fs: FileSymbols) -> str:
    """Format one file's symbols in source order, methods indented under their class."""
    lines = [f"{fs.path}:"]
    for sym in fs.symbols[:_MAX_SYMBOLS_PER_FILE]:
        indent = "    " if sym.kind == "method" else "  "
        lines.append(f"{indent}{sym.signature}")
    hidden = len(fs.symbols) - _MAX_SYMBOLS_PER_FILE
    if hidden > 0:
        lines.append(f"  ... +{hidden} more")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tree-sitter backend (optional)
# ---------------------------------------------------------------------------

# Additional extensions supported by tree-sitter
_TREE_SITTER_EXTS = {
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".cs",
    ".swift",
    ".dart",
    ".scala",
    ".ex",
    ".exs",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".lua",
    ".zig",
}

# Language extensions for basic mode (no tree-sitter)
_LANG_EXTS: dict[str, set[str]] = {
    "javascript": {".js", ".jsx"},
    "typescript": {".ts", ".tsx"},
    "go": {".go"},
    "rust": {".rs"},
    "java": {".java"},
    "kotlin": {".kt"},
    "ruby": {".rb"},
    "php": {".php"},
    "csharp": {".cs"},
    "c++": {".cpp", ".hpp", ".h"},
    "c": {".c", ".h"},
    "swift": {".swift"},
}


def _extract_tree_sitter_symbols(full_path: Path, rel_path: str) -> FileSymbols | None:
    """Extract symbols using tree-sitter-language-pack."""
    try:
        from tree_sitter_language_pack import get_parser

        ext = full_path.suffix
        lang_map: dict[str, str] = {
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".kt": "kotlin",
            ".rb": "ruby",
            ".php": "php",
            ".cs": "c_sharp",
            ".swift": "swift",
            ".dart": "dart",
            ".scala": "scala",
            ".ex": "elixir",
            ".exs": "elixir",
            ".c": "c",
            ".cpp": "cpp",
            ".h": "c",
            ".hpp": "cpp",
            ".lua": "lua",
            ".zig": "zig",
        }

        lang = lang_map.get(ext)
        if not lang:
            return None

        source = full_path.read_bytes()
        parser = get_parser(cast(Any, lang))  # the language pack types its names as a Literal
        tree = parser.parse(source)

        result = FileSymbols(path=rel_path)

        # Walk the tree for class/function definitions
        _walk_tree_sitter(tree.root_node, source, rel_path, result)

        return result if result.symbols else None

    except Exception as e:
        logger.debug("tree-sitter parse failed for %s: %s", rel_path, e)
        return None


def _walk_tree_sitter(
    node: Any,
    source: bytes,
    rel_path: str,
    result: FileSymbols,
    parent_class: str = "",
) -> None:
    """Walk a tree-sitter AST node and extract symbol definitions."""
    node_type = node.type

    # Class definitions
    if node_type in (
        "class_definition",
        "class_declaration",
        "struct_item",
        "impl_item",
        "interface_declaration",
        "type_declaration",
    ):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte : name_node.end_byte].decode(errors="replace")
            result.symbols.append(
                Symbol(
                    name=name,
                    kind="class",
                    file=rel_path,
                    line=node.start_point[0] + 1,
                    signature=f"class {name}",
                )
            )
            # Recurse into class body for methods
            for child in node.children:
                _walk_tree_sitter(child, source, rel_path, result, parent_class=name)
            return

    # Function/method definitions
    if node_type in (
        "function_definition",
        "function_declaration",
        "method_definition",
        "method_declaration",
        "function_item",  # Rust
        "arrow_function",
    ):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = source[name_node.start_byte : name_node.end_byte].decode(errors="replace")
            # Get the first line as signature
            start = node.start_byte
            end = source.find(b"\n", start)
            if end == -1:
                end = node.end_byte
            sig = source[start : min(end, start + 120)].decode(errors="replace").strip()

            kind = "method" if parent_class else "function"
            result.symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    file=rel_path,
                    line=node.start_point[0] + 1,
                    signature=sig,
                    parent=parent_class,
                )
            )
            return

    # Recurse into children
    for child in node.children:
        _walk_tree_sitter(child, source, rel_path, result, parent_class=parent_class)
