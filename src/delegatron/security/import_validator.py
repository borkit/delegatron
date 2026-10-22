"""Post-patch import validator (multi-language).

Rejects (or warns about) patches whose imports reference packages that
aren't actually installed in the project. Catches the common failure
mode where a 14B model confidently writes
``import userEvent from '@testing-library/user-event'`` against a project
where that library isn't in ``package.json.devDependencies``, or
``import numpy`` in a project with no ``numpy`` in ``requirements.txt``.

Supported languages (by file extension + package config):
- JavaScript / TypeScript (.js .jsx .mjs .cjs .ts .tsx) vs ``package.json``
- Python (.py) vs ``pyproject.toml`` / ``requirements.txt`` + stdlib
- Go (.go) vs ``go.mod`` require directives + stdlib heuristic
- Rust (.rs) vs ``Cargo.toml`` [dependencies] + std crates
- Java (.java) — best-effort; Maven imports don't map cleanly to
  groupId/artifactId, so we only flag obvious cases

The patch is overlaid on the workspace: source files and package configs
the patch writes count as present, so the result is the same whether the
validator runs before or after the patch is applied.

Validation is advisory: returns a list of issues rather than raising.
The pipeline stores them as ``import_issues``; they reach the next
implementer as ``import_warnings`` only after a failed test run, and the
reviewer as advisory input.  They never gate acceptance on their own.
"""

from __future__ import annotations

import ast
import configparser
import contextlib
import json
import os
import posixpath
import re
import sys
import tomllib
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from delegatron.security.patch_lines import decode_text
from delegatron.utils.paths import normalize_rel_path

# =============================================================================
# Patch overlay
# =============================================================================


def _read_project_file(workspace_root: Path, files: Mapping[str, str], rel: str) -> str | None:
    """Return a project file's text, preferring the patch's version over disk.

    ``None`` when neither the patch nor the workspace has the file.
    """
    if rel in files:
        return files[rel]
    path = workspace_root / rel
    if not path.is_file():
        return None
    try:
        return decode_text(path.read_bytes())
    except OSError:
        return None


# =============================================================================
# JS / TS
# =============================================================================

# Node stdlib / built-ins — always available, never need installing.
_NODE_BUILTINS = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "fs/promises",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)

_JS_IMPORT_RE = re.compile(
    r"""
    (?:
        ^\s*import\s+[^'";]*from\s+['"]([^'"]+)['"]     # import X from 'Y'
        | ^\s*import\s+['"]([^'"]+)['"]                  # import 'Y'  (side-effect)
        | \brequire\(\s*['"]([^'"]+)['"]\s*\)            # require('Y')
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

_JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")

# Root configs whose ``compilerOptions.paths`` define import aliases.
_JS_ALIAS_CONFIGS = ("tsconfig.json", "jsconfig.json")


def _strip_jsonc(text: str) -> str:
    """Return JSONC *text* as JSON: ``//`` and ``/* */`` comments and trailing commas removed.

    String-aware, so a key such as ``"@/*"`` (which contains ``/*``) or a
    string value holding ``//`` is kept intact.
    """
    out: list[str] = []
    i, length = 0, len(text)
    in_string = False
    while i < length:
        ch = text[i]
        if in_string:
            if ch == "\\":
                out.append(text[i : i + 2])
                i += 2
                continue
            in_string = ch != '"'
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = length if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = length if end == -1 else end + 2
            out.append(" ")
        else:
            if ch in "}]":
                _drop_trailing_comma(out)
            in_string = ch == '"'
            out.append(ch)
            i += 1
    return "".join(out)


def _drop_trailing_comma(out: list[str]) -> None:
    """Remove a ``,`` that is the last non-whitespace output before a closing bracket."""
    j = len(out) - 1
    while j >= 0 and out[j].isspace():
        j -= 1
    if j >= 0 and out[j] == ",":
        del out[j]


def _js_path_aliases(
    workspace_root: Path, files: Mapping[str, str]
) -> tuple[frozenset[str], tuple[str, ...]]:
    """``(exact_keys, wildcard_prefixes)`` from ``compilerOptions.paths``.

    Read from the root ``tsconfig.json`` and ``jsconfig.json`` (JSONC).
    ``"@app/*"`` gives the prefix ``@app/`` and ``"config"`` the exact key
    ``config``; a bare ``"*"`` is ignored.  A config that does not parse
    contributes nothing; ``extends`` and ``baseUrl`` are not followed.
    """
    exact: set[str] = set()
    prefixes: set[str] = set()
    for rel in _JS_ALIAS_CONFIGS:
        text = _read_project_file(workspace_root, files, rel)
        if text is None:
            continue
        try:
            data = json.loads(_strip_jsonc(text))
        except (ValueError, RecursionError):
            continue
        options = data.get("compilerOptions") if isinstance(data, dict) else None
        paths = options.get("paths") if isinstance(options, dict) else None
        if not isinstance(paths, dict):
            continue
        for key in paths:
            if not isinstance(key, str):
                continue
            if "*" in key:
                prefix = key.split("*", 1)[0]
                if prefix:
                    prefixes.add(prefix)
            elif key:
                exact.add(key)
    return frozenset(exact), tuple(sorted(prefixes))


def _is_path_alias(spec: str, aliases: tuple[frozenset[str], tuple[str, ...]]) -> bool:
    """True when *spec* equals an exact alias key or starts with a wildcard prefix."""
    exact, prefixes = aliases
    return spec in exact or spec.startswith(prefixes)


# =============================================================================
# Python
# =============================================================================

# PyPI install-name → import-name mapping for common mismatched cases.
# Keyed by normalized install name (lowercase, underscores → hyphens).
# Kept short: only packages where the import differs significantly from the
# install name.  When the install name == import name, no entry needed.
_PY_INSTALL_TO_IMPORT: dict[str, tuple[str, ...]] = {
    "pyyaml": ("yaml",),
    "pillow": ("pil",),
    "scikit-learn": ("sklearn",),
    "beautifulsoup4": ("bs4",),
    "opencv-python": ("cv2",),
    "opencv-python-headless": ("cv2",),
    "protobuf": ("google",),  # google.protobuf.*
    "msgpack-python": ("msgpack",),
    "python-dateutil": ("dateutil",),
    "python-dotenv": ("dotenv",),
    "python-jose": ("jose",),
    "pycryptodome": ("crypto",),
    "pycryptodomex": ("cryptodome",),
    "google-genai": ("google",),  # google.genai
    "google-cloud-storage": ("google",),
    "pymongo": ("pymongo", "bson"),
    "ruamel-yaml": ("ruamel",),
    "gitpython": ("git",),
}

# Importable in every Python sandbox: the install step always adds pytest
# (factories/sandbox_factory.get_install_commands).
_PY_SANDBOX_PROVIDED = frozenset({"pytest", "_pytest"})

# Requirement files read for declared deps: root ``requirements*.txt`` and
# ``requirements/*.txt`` (posix, relative to the workspace root).
_PY_REQUIREMENT_FILE_RE = re.compile(r"^(?:requirements[^/]*|requirements/[^/]+)\.txt$")
# `-r X`, `-rX`, `--requirement X`, `--requirement=X` include another file.
_PY_REQUIREMENT_INCLUDE_RE = re.compile(r"^(?:-r|--requirement)(?:\s*=\s*|\s*)(\S+)")

_PY_IMPORT_RE = re.compile(
    r"""
    ^\s*
    (?:
        import\s+([\w.]+)              # import X.Y
        | from\s+(\.+)?                 # relative dots (optional)
          ([\w.]*)                      # from X.Y
          \s+import\s+
    )
    """,
    re.VERBOSE | re.MULTILINE,
)


def _py_normalize(name: str) -> str:
    """PyPI normalization: lowercase + _/. → -"""
    return re.sub(r"[-_.]+", "-", name.lower())


def _py_stdlib() -> frozenset[str]:
    # sys.stdlib_module_names is available on Python 3.10+
    if hasattr(sys, "stdlib_module_names"):
        return frozenset(sys.stdlib_module_names)
    return frozenset()


def _py_dir_has_code(path: Path) -> bool:
    """True for a regular or namespace package: a directory holding any ``.py`` file."""
    for _dirpath, dirnames, filenames in os.walk(path):
        if any(name.endswith(".py") for name in filenames):
            return True
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
    return False


def _py_module_in(
    base: PurePosixPath,
    name: str,
    workspace_root: Path,
    patch_py: frozenset[str],
) -> bool:
    """True if ``base/<name>.py`` or a package dir ``base/<name>/`` exists.

    Checks the patch's own ``.py`` paths, then the disk.  A directory counts
    with or without ``__init__.py`` (namespace package).
    """
    module = (base / f"{name}.py").as_posix()
    package = (base / name).as_posix() + "/"
    if module in patch_py or any(path.startswith(package) for path in patch_py):
        return True
    disk = workspace_root / base.as_posix()
    return (disk / f"{name}.py").is_file() or _py_dir_has_code(disk / name)


def _py_is_local(
    name: str,
    importer_dir: PurePosixPath,
    workspace_root: Path,
    patch_py: frozenset[str],
) -> bool:
    """True if top-level module ``name`` resolves to project code.

    Searches the workspace root and ``src/`` (``python -m pytest`` rootdir,
    src layout) plus the importing file's own directory (pytest's prepend
    import mode, ``python scripts/x.py``).  ``_private`` modules and
    ``conftest`` are always treated as local.
    """
    if name.startswith("_") or name == "conftest":
        return True
    bases = {PurePosixPath(), PurePosixPath("src"), importer_dir}
    return any(_py_module_in(base, name, workspace_root, patch_py) for base in bases)


def _toml_table(data: object, *keys: str) -> dict[str, Any]:
    """Return the nested TOML table at ``keys``, or ``{}`` if absent or not a table."""
    node = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _pyproject_deps(data: dict[str, Any]) -> set[str]:
    """Normalized dependency names declared in a parsed ``pyproject.toml``.

    Covers PEP 621 ``dependencies`` / ``optional-dependencies``, PEP 735
    ``[dependency-groups]``, Poetry's main, dev and ``group.*`` tables,
    ``[tool.pdm.dev-dependencies]`` and ``[tool.uv] dev-dependencies``.
    """
    project = _toml_table(data, "project")
    spec_lists: list[Any] = [project.get("dependencies")]
    spec_lists.extend(_toml_table(project, "optional-dependencies").values())
    spec_lists.extend(_toml_table(data, "dependency-groups").values())
    spec_lists.extend(_toml_table(data, "tool", "pdm", "dev-dependencies").values())
    spec_lists.append(_toml_table(data, "tool", "uv").get("dev-dependencies"))
    declared = {
        _py_name_from_req_spec(spec)
        for specs in spec_lists
        if isinstance(specs, list)
        for spec in specs
        # Skips PEP 735 {include-group = "..."} entries and pip options ("-e ...").
        if isinstance(spec, str) and not spec.lstrip().startswith("-")
    }

    poetry = _toml_table(data, "tool", "poetry")
    poetry_tables = [_toml_table(poetry, "dependencies"), _toml_table(poetry, "dev-dependencies")]
    poetry_tables.extend(
        _toml_table(group, "dependencies") for group in _toml_table(poetry, "group").values()
    )
    declared.update(
        _py_normalize(name) for table in poetry_tables for name in table if name.lower() != "python"
    )
    return declared


def _setup_cfg_deps(text: str) -> set[str]:
    """Normalized names from setup.cfg ``install_requires`` and ``extras_require``."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return set()
    specs = parser.get("options", "install_requires", fallback="").splitlines()
    if parser.has_section("options.extras_require"):
        for _extra, value in parser.items("options.extras_require"):
            specs.extend(value.splitlines())
    return {_py_name_from_req_spec(spec) for spec in specs if spec.strip()}


def _py_requirement_files(workspace_root: Path, files: Mapping[str, str]) -> list[str]:
    """Root ``requirements*.txt`` and ``requirements/*.txt`` in the patch or on disk."""
    found = {path for path in files if _PY_REQUIREMENT_FILE_RE.match(path)}
    with contextlib.suppress(OSError):
        found.update(p.name for p in workspace_root.glob("requirements*.txt") if p.is_file())
        req_dir = workspace_root / "requirements"
        if req_dir.is_dir():
            found.update(f"requirements/{p.name}" for p in req_dir.glob("*.txt") if p.is_file())
    return sorted(found)


def _py_include_target(including: str, target: str, workspace_root: Path) -> str | None:
    """Workspace-relative path of a ``-r`` include, or None when it leaves the workspace."""
    target = target.strip("\"'").replace("\\", "/")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(including), target))
    rel = None if joined == ".." or joined.startswith("../") else normalize_rel_path(joined)
    if rel is None:
        return None
    try:  # a symlink must not lead outside the workspace either
        root = workspace_root.resolve()
        inside = (root / rel).resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return None
    return rel if inside else None


def _py_requirements_deps(
    rel: str, workspace_root: Path, files: Mapping[str, str], seen: set[str]
) -> set[str]:
    """Normalized names a requirements file declares, following ``-r`` includes.

    Includes resolve relative to the including file and are read only inside
    *workspace_root*; *seen* stops include cycles.
    """
    if rel in seen:
        return set()
    seen.add(rel)
    text = _read_project_file(workspace_root, files, rel) or ""
    declared: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        include = _PY_REQUIREMENT_INCLUDE_RE.match(line)
        if include:
            target = _py_include_target(rel, include.group(1), workspace_root)
            if target is not None:
                declared.update(_py_requirements_deps(target, workspace_root, files, seen))
        elif not line.startswith("-"):
            declared.add(_py_name_from_req_spec(line))
    return declared


def _py_declared_deps(workspace_root: Path, files: Mapping[str, str]) -> set[str]:
    """Parse declared dependencies from pyproject.toml, setup.cfg and requirements files.

    Returns a set of normalized PyPI names (lowercase, hyphens).  Also
    includes the corresponding import-name aliases from the hardcoded
    mapping so ``import yaml`` resolves when ``PyYAML`` is declared.
    """
    declared: set[str] = set()

    pyproject = _read_project_file(workspace_root, files, "pyproject.toml")
    if pyproject is not None:
        try:
            data = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            data = {}
        declared.update(_pyproject_deps(data))

    setup_cfg = _read_project_file(workspace_root, files, "setup.cfg")
    if setup_cfg is not None:
        declared.update(_setup_cfg_deps(setup_cfg))

    seen: set[str] = set()
    for rel in _py_requirement_files(workspace_root, files):
        declared.update(_py_requirements_deps(rel, workspace_root, files, seen))

    declared.discard("")

    # Expand install-name aliases so `import yaml` satisfies a `PyYAML` dep.
    expanded = set(declared)
    for install_name in list(declared):
        for alias in _PY_INSTALL_TO_IMPORT.get(install_name, ()):
            expanded.add(_py_normalize(alias))
    return expanded


def _py_name_from_req_spec(spec: str) -> str:
    """Extract the package name from a requirement line.

    `numpy>=1.20`  → 'numpy'
    `pytest[test]` → 'pytest'
    `git+https://github.com/foo/bar.git#egg=bar` → 'bar'
    """
    spec = spec.split(";", 1)[0].strip()  # drop environment markers
    if spec.startswith(("http://", "https://", "git+", "file:")):
        if "#egg=" in spec:
            spec = spec.split("#egg=", 1)[1]
        else:
            return ""
    # Strip extras `pkg[x,y]` → `pkg`
    spec = re.sub(r"\[[^\]]*\]", "", spec)
    # Strip version specifiers
    spec = re.split(r"[<>=!~\s]", spec, maxsplit=1)[0].strip()
    return _py_normalize(spec)


def _extract_py_imports(content: str) -> set[tuple[str, bool]]:
    """Return ``{(top_level_module, is_relative)}`` for the file's absolute imports.

    Parsed with ``ast`` so imports quoted in strings or docstrings are
    ignored; falls back to a line regex when the file doesn't parse.
    Relative imports are skipped (resolved at runtime against the package
    layout, so always valid locally).
    """
    try:
        with warnings.catch_warnings():
            # Model-written code often has invalid escapes such as "\d".
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return _extract_py_imports_regex(content)

    mods: set[tuple[str, bool]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update((alias.name.split(".", 1)[0], False) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add((node.module.split(".", 1)[0], False))
    return mods


def _extract_py_imports_regex(content: str) -> set[tuple[str, bool]]:
    """Line-based fallback for :func:`_extract_py_imports` on unparsable files."""
    mods: set[tuple[str, bool]] = set()
    for m in _PY_IMPORT_RE.finditer(content):
        imp, dots, frm = m.group(1), m.group(2), m.group(3)
        if dots:
            # Relative import — skip; resolved at runtime against pkg layout.
            continue
        target = imp or frm
        if not target:
            continue
        top = target.split(".", 1)[0]
        if top:
            mods.add((top, False))
    return mods


def _py_validate(
    files: dict[str, str],
    workspace_root: Path,
) -> list[ImportIssue]:
    stdlib = _py_stdlib()
    declared = _py_declared_deps(workspace_root, files)
    patch_py = frozenset(path for path in files if path.endswith(".py"))
    # If the project declared literally no deps, we can't tell missing imports
    # from a project that hasn't set up its deps yet.  Still useful to flag
    # clearly-missing imports in that case though: if stdlib/local doesn't
    # cover it, warn.
    issues: list[ImportIssue] = []
    for rel_path, content in files.items():
        if not rel_path.endswith(".py"):
            continue
        importer_dir = PurePosixPath(rel_path).parent
        for top, _is_rel in sorted(_extract_py_imports(content)):
            if top in stdlib or top in _PY_SANDBOX_PROVIDED:
                continue
            # Declared deps already include install-name aliases.
            if _py_normalize(top) in declared:
                continue
            if _py_is_local(top, importer_dir, workspace_root, patch_py):
                continue
            issues.append(
                ImportIssue(
                    file=rel_path,
                    module=top,
                    severity="blocker" if declared else "warning",
                    message=(
                        f"Python module `{top}` is not in pyproject.toml / "
                        f"requirements.txt and isn't stdlib or a local module."
                        + (
                            f" Declared deps: {', '.join(sorted(declared)[:10])}"
                            + ("..." if len(declared) > 10 else "")
                            if declared
                            else " (no declared deps found — if it is meant to be a"
                            f" local module, create `{top}.py`; otherwise add it to"
                            " requirements.txt)"
                        )
                    ),
                )
            )
    return issues


# =============================================================================
# Go
# =============================================================================

_GO_IMPORT_BLOCK_RE = re.compile(r"import\s*\(([^)]*)\)", re.DOTALL)
_GO_IMPORT_SINGLE_RE = re.compile(r'^\s*import\s+"([^"]+)"', re.MULTILINE)
_GO_STR_RE = re.compile(r'"([^"]+)"')


def _extract_go_imports(content: str) -> set[str]:
    """Return all imported module paths."""
    mods: set[str] = set()
    for block in _GO_IMPORT_BLOCK_RE.findall(content):
        for m in _GO_STR_RE.finditer(block):
            mods.add(m.group(1))
    for m in _GO_IMPORT_SINGLE_RE.finditer(content):
        mods.add(m.group(1))
    return mods


def _go_is_stdlib(path: str) -> bool:
    """Go stdlib paths have no dot in the first component (fmt, net/http)."""
    return "." not in path.split("/", 1)[0]


def _go_declared_modules(
    workspace_root: Path, files: Mapping[str, str]
) -> tuple[set[str], str | None]:
    """Return ``(required_modules, own_module_path)`` from go.mod."""
    text = _read_project_file(workspace_root, files, "go.mod")
    if text is None:
        return set(), None

    own: str | None = None
    mods: set[str] = set()
    m = re.search(r"^\s*module\s+(\S+)", text, re.MULTILINE)
    if m:
        own = m.group(1)

    # Single-line: `require github.com/foo/bar v1.0.0`
    for m in re.finditer(r"^\s*require\s+(\S+)\s+\S+", text, re.MULTILINE):
        mods.add(m.group(1))
    # Block: `require ( github.com/... v1 \n ... )`
    for block in re.findall(r"require\s*\(([^)]*)\)", text, re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            mod = line.split()[0] if line.split() else ""
            if mod:
                mods.add(mod)
    return mods, own


def _go_validate(
    files: dict[str, str],
    workspace_root: Path,
) -> list[ImportIssue]:
    declared, own = _go_declared_modules(workspace_root, files)
    if not declared and own is None:
        return []  # no go.mod — can't validate
    issues: list[ImportIssue] = []
    for rel_path, content in files.items():
        if not rel_path.endswith(".go"):
            continue
        for imp in _extract_go_imports(content):
            if _go_is_stdlib(imp):
                continue
            # Imports from own module are fine
            if own and (imp == own or imp.startswith(own + "/")):
                continue
            # Check against declared; accept subpaths of a declared module
            if any(imp == d or imp.startswith(d + "/") for d in declared):
                continue
            issues.append(
                ImportIssue(
                    file=rel_path,
                    module=imp,
                    severity="blocker",
                    message=(
                        f"Go module `{imp}` is not in go.mod require "
                        f"directives. Add `require {imp} vX.Y.Z` or remove "
                        f"the import."
                    ),
                )
            )
    return issues


# =============================================================================
# Rust
# =============================================================================

_RUST_STD_CRATES = frozenset({"std", "core", "alloc", "proc_macro", "test"})
# `use foo::bar::Baz;` or `use foo;` or `extern crate foo;`
_RUST_USE_RE = re.compile(r"^\s*use\s+(\w+)(?:::|;)", re.MULTILINE)
_RUST_EXTERN_RE = re.compile(r"^\s*extern\s+crate\s+(\w+)", re.MULTILINE)
# Items the crate declares itself: `mod cards;`, `pub(crate) enum Color {`, ...
_RUST_ITEM_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:mod|enum|struct|trait|type|union)\s+(\w+)",
    re.MULTILINE,
)


def _extract_rust_crates(content: str) -> set[str]:
    crates: set[str] = set()
    crates.update(_RUST_USE_RE.findall(content))
    crates.update(_RUST_EXTERN_RE.findall(content))
    return crates


_CARGO_DEP_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")


def _rust_local_names(content: str) -> set[str]:
    """Module and type names declared in one ``.rs`` file.

    Rust 2018 ``use`` paths may start with an item in the file's own scope
    rather than a crate: ``mod cards; use cards::Card;`` or
    ``enum Color {..} use Color::*;``.  A declaration in another file is not
    in scope, so this is checked per file.
    """
    return set(_RUST_ITEM_RE.findall(content))


def _cargo_dep_names(table: object) -> set[str]:
    """Normalized crate names (``-`` → ``_``) of the dependency sections in *table*."""
    names: set[str] = set()
    for section in _CARGO_DEP_SECTIONS:
        deps = table.get(section) if isinstance(table, dict) else None
        if isinstance(deps, dict):
            names.update(name.replace("-", "_") for name in deps if isinstance(name, str))
    return names


def _rust_declared_crates(
    workspace_root: Path, files: Mapping[str, str]
) -> tuple[set[str], str | None]:
    """Return ``(dep_names, own_crate_name)`` from Cargo.toml.

    Dependencies come from the top-level sections and from every
    ``[target.<cfg>]`` table.
    """
    text = _read_project_file(workspace_root, files, "Cargo.toml")
    if text is None:
        return set(), None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set(), None
    own = _toml_table(data, "package").get("name")
    own_norm = own.replace("-", "_") if isinstance(own, str) else None
    deps = _cargo_dep_names(data)
    for cfg_table in _toml_table(data, "target").values():
        deps |= _cargo_dep_names(cfg_table)
    return deps, own_norm


def _rust_validate(
    files: dict[str, str],
    workspace_root: Path,
) -> list[ImportIssue]:
    declared, own = _rust_declared_crates(workspace_root, files)
    if not declared and own is None:
        return []
    issues: list[ImportIssue] = []
    allowed = declared | _RUST_STD_CRATES | {"self", "super", "crate"}
    if own:
        allowed.add(own)
    for rel_path, content in files.items():
        if not rel_path.endswith(".rs"):
            continue
        local = _rust_local_names(content)
        for crate in _extract_rust_crates(content):
            # Crate names are snake_case; an uppercase root is a type or an
            # enum in scope (`use Color::{Red, Green};`, `use Direction::*;`).
            if crate in allowed or crate in local or crate[:1].isupper():
                continue
            issues.append(
                ImportIssue(
                    file=rel_path,
                    module=crate,
                    severity="blocker",
                    message=(
                        f"Rust crate `{crate}` is not in Cargo.toml "
                        f"[dependencies] and isn't a std crate. Add it or "
                        f"remove the use statement."
                    ),
                )
            )
    return issues


# =============================================================================
# Java (best-effort: groupId/artifactId don't map cleanly to import paths)
# =============================================================================

# JDK / platform packages — always available.  Not exhaustive; covers
# everything a local model is likely to pull in.
_JAVA_JDK_PREFIXES = (
    "java.",
    "javax.",
    "jdk.",
    "sun.",
    "com.sun.",
    "org.w3c.dom",
    "org.xml.sax",
    "org.ietf.jgss",
)

# Common test/util packages that ship via well-known Maven artifacts.
# Map import-prefix → artifactId (for the hint only — we don't strictly
# require the artifact is present since Java imports don't cleanly resolve).
_JAVA_WELL_KNOWN = {
    "org.junit.jupiter.": "junit-jupiter",
    "org.junit.": "junit",
    "org.mockito.": "mockito-core",
    "org.hamcrest.": "hamcrest",
    "org.assertj.": "assertj-core",
    "com.fasterxml.jackson.": "jackson-core",
    "com.google.gson.": "gson",
    "com.google.common.": "guava",
    "org.apache.commons.": "commons-*",
    "org.slf4j.": "slf4j-api",
    "lombok.": "lombok",
}

_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", re.MULTILINE)


def _extract_java_imports(content: str) -> set[str]:
    """Return imported FQCNs (or package prefixes when ``.*`` is used)."""
    return set(_JAVA_IMPORT_RE.findall(content))


def _java_pom_artifacts(workspace_root: Path, files: Mapping[str, str]) -> set[str]:
    """Extract <artifactId> values from pom.xml dependencies.  Rough parse;
    avoids taking an XML dep for a best-effort check."""
    text = _read_project_file(workspace_root, files, "pom.xml")
    if text is None:
        return set()
    return set(re.findall(r"<artifactId>([^<]+)</artifactId>", text))


def _java_validate(
    files: dict[str, str],
    workspace_root: Path,
) -> list[ImportIssue]:
    artifacts = _java_pom_artifacts(workspace_root, files)
    if not artifacts:
        # No pom.xml OR empty — skip.  Gradle isn't parsed here.
        return []
    issues: list[ImportIssue] = []
    for rel_path, content in files.items():
        if not rel_path.endswith(".java"):
            continue
        for fqcn in _extract_java_imports(content):
            if fqcn.startswith(_JAVA_JDK_PREFIXES):
                continue
            matched_artifact = None
            for prefix, artifact in _JAVA_WELL_KNOWN.items():
                if fqcn.startswith(prefix):
                    matched_artifact = artifact
                    break
            if matched_artifact:
                # Check if a related artifact is in pom.  For wildcards
                # like `commons-*`, accept any commons- prefix.
                if matched_artifact.endswith("*"):
                    base = matched_artifact[:-1]
                    if any(a.startswith(base) for a in artifacts):
                        continue
                elif matched_artifact in artifacts or any(matched_artifact in a for a in artifacts):
                    continue
                issues.append(
                    ImportIssue(
                        file=rel_path,
                        module=fqcn,
                        severity="warning",
                        message=(
                            f"Java import `{fqcn}` typically comes from the "
                            f"`{matched_artifact}` artifact, which isn't in "
                            f"pom.xml <dependencies>."
                        ),
                    )
                )
            # For imports we don't recognize (user's own packages, obscure
            # libraries), stay silent — too many false positives.  Java
            # imports don't map cleanly to Maven coordinates in general.
    return issues


# =============================================================================
# Top-level dispatcher
# =============================================================================


@dataclass
class ImportIssue:
    """One unresolved import found in a patch."""

    file: str
    module: str
    severity: Literal["blocker", "warning"]
    message: str


def _extract_js_imports(content: str) -> set[str]:
    """Return the set of module specifiers imported in a JS/TS file."""
    mods: set[str] = set()
    for m in _JS_IMPORT_RE.finditer(content):
        spec = m.group(1) or m.group(2) or m.group(3)
        if spec:
            mods.add(spec)
    return mods


def _is_bare_specifier(spec: str) -> bool:
    """True if the import references an npm package.

    Relative/absolute paths, ``#`` subpath imports, ``@/`` and ``~/`` path
    aliases and scheme specifiers are not packages.  The ``node:`` scheme
    (``node:fs``, ``node:test``) only ever resolves to a Node built-in.
    """
    if spec.startswith((".", "/", "#", "@/", "~/")):
        return False
    # npm package names never contain ":"; `node:`, `https:` etc. do.
    return ":" not in spec


def _package_name_from_specifier(spec: str) -> str:
    """Strip subpath: '@scope/pkg/sub/thing' -> '@scope/pkg', 'react/jsx-runtime' -> 'react'."""
    if spec.startswith("@"):
        parts = spec.split("/", 2)
        return "/".join(parts[:2]) if len(parts) >= 2 else spec
    return spec.split("/", 1)[0]


def _load_package_json_deps(workspace_root: Path, files: Mapping[str, str]) -> set[str] | None:
    """Return the merged set of dep names from package.json, or None if missing."""
    text = _read_project_file(workspace_root, files, "package.json")
    if text is None:
        return None
    try:
        pkg = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(pkg, dict):
        return None

    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            deps.update(k for k in section if isinstance(k, str))
    return deps


def _js_validate(
    files: dict[str, str],
    workspace_root: Path,
) -> list[ImportIssue]:
    deps = _load_package_json_deps(workspace_root, files)
    if deps is None:
        return []
    aliases = _js_path_aliases(workspace_root, files)
    issues: list[ImportIssue] = []
    for rel_path, content in files.items():
        if not rel_path.endswith(_JS_EXTS):
            continue
        for spec in _extract_js_imports(content):
            if not _is_bare_specifier(spec) or _is_path_alias(spec, aliases):
                continue
            pkg_name = _package_name_from_specifier(spec)
            if pkg_name in _NODE_BUILTINS:
                continue
            if pkg_name in deps:
                continue
            issues.append(
                ImportIssue(
                    file=rel_path,
                    module=spec,
                    severity="blocker",
                    message=(
                        f"`{spec}` is not in package.json "
                        f"(dependencies / devDependencies). "
                        f"Either remove the import or pick an already-installed "
                        f"alternative. Available: "
                        f"{', '.join(sorted(deps)[:20])}" + ("..." if len(deps) > 20 else "")
                    ),
                )
            )
    return issues


def validate_patch_imports(
    patch: dict[str, object],
    workspace_root: Path,
) -> list[ImportIssue]:
    """Scan patch files for imports that can't be resolved.

    Dispatches by file extension to per-language handlers.  Each handler
    returns issues independently; results are concatenated.  Safe to call
    on a polyglot patch, and gives the same answer before or after the
    patch is written to ``workspace_root``.
    """
    files = patch.get("files")
    if not isinstance(files, dict):
        return []
    # Filter to valid (str, str) entries upfront.  Keys become posix paths
    # so "./a.py" and "tests\\b.py" match the overlay lookups.
    typed_files: dict[str, str] = {
        PurePosixPath(k.replace("\\", "/")).as_posix(): v
        for k, v in files.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    if not typed_files:
        return []

    issues: list[ImportIssue] = []
    issues.extend(_js_validate(typed_files, workspace_root))
    issues.extend(_py_validate(typed_files, workspace_root))
    issues.extend(_go_validate(typed_files, workspace_root))
    issues.extend(_rust_validate(typed_files, workspace_root))
    issues.extend(_java_validate(typed_files, workspace_root))
    return issues
