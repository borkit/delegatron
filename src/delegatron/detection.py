"""Project language, framework, and tooling detection.

Scans a workspace to determine the project's stack so that agents
receive appropriate context about what they're working with.
"""

from __future__ import annotations

import codecs
import fnmatch
import os
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from delegatron.sandbox.toolchains import (
    CMAKE_TEST_COMMAND,
    PESTER_TEST_COMMAND,
    PYTEST_COMMAND,
)
from delegatron.utils.paths import internal_dirs_for


@dataclass
class ProjectInfo:
    """Detected project metadata."""

    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    lint_commands: list[str] = field(default_factory=list)
    package_manager: str = ""
    entry_points: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)


_PYTEST = PYTEST_COMMAND

_Rule = tuple[str, str, str, str, str, str]

# Detection rules: (marker file, language, framework, test_cmd, lint_cmd, pkg_manager)
_DETECTION_RULES: list[_Rule] = [
    # Python — ``python -m pytest`` puts the project root on sys.path, so
    # tests/ can import root-level modules (bare ``pytest`` cannot).
    ("pyproject.toml", "python", "", _PYTEST, "ruff check .", "pip"),
    ("setup.py", "python", "", _PYTEST, "", "pip"),
    ("setup.cfg", "python", "", _PYTEST, "", "pip"),
    ("Pipfile", "python", "", _PYTEST, "", "pipenv"),
    ("poetry.lock", "python", "", _PYTEST, "", "poetry"),
    ("requirements.txt", "python", "", _PYTEST, "", "pip"),
    # Python frameworks
    ("manage.py", "python", "django", "python manage.py test", "", ""),
    # JavaScript / TypeScript
    ("package.json", "javascript", "", "npm test", "npm run lint", "npm"),
    ("yarn.lock", "javascript", "", "yarn test", "yarn lint", "yarn"),
    ("pnpm-lock.yaml", "javascript", "", "pnpm test", "pnpm lint", "pnpm"),
    ("bun.lockb", "javascript", "", "bun test", "bun lint", "bun"),
    ("tsconfig.json", "typescript", "", "", "", ""),
    # Go
    ("go.mod", "go", "", "go test ./...", "golangci-lint run", "go"),
    # Rust
    ("Cargo.toml", "rust", "", "cargo test", "cargo clippy", "cargo"),
    # Java / Kotlin
    ("pom.xml", "java", "maven", "mvn test", "", "maven"),
    ("build.gradle", "java", "gradle", "gradle test", "", "gradle"),
    ("build.gradle.kts", "kotlin", "gradle", "gradle test", "", "gradle"),
    # Ruby
    ("Gemfile", "ruby", "", "bundle exec rspec", "bundle exec rubocop", "bundler"),
    # PHP
    ("composer.json", "php", "", "vendor/bin/phpunit", "", "composer"),
    # .NET
    ("*.sln", "csharp", "dotnet", "dotnet test", "", "dotnet"),
    ("*.csproj", "csharp", "dotnet", "dotnet test", "", "dotnet"),
    ("*.fsproj", "fsharp", "dotnet", "dotnet test", "", "dotnet"),
    # PowerShell
    ("*.ps1", "powershell", "", PESTER_TEST_COMMAND, "", ""),
    # Shell
    ("*.sh", "shell", "", "bash -n *.sh", "", ""),
    # C/C++
    ("CMakeLists.txt", "c++", "cmake", CMAKE_TEST_COMMAND, "", "cmake"),
    ("Makefile", "c", "", "make test", "", "make"),
    # Swift
    ("Package.swift", "swift", "", "swift test", "", "swift"),
    # Dart / Flutter
    ("pubspec.yaml", "dart", "", "dart test", "", "dart"),
    # Elixir
    ("mix.exs", "elixir", "", "mix test", "", "mix"),
    # Scala
    ("build.sbt", "scala", "", "sbt test", "", "sbt"),
    # Haskell
    ("stack.yaml", "haskell", "", "stack test", "", "stack"),
]

# Script and build helpers that repos of any language carry.  They decide
# the language only when no other marker matched.
_AUXILIARY_MARKERS = frozenset({"*.ps1", "*.sh", "Makefile"})

# A Python project without any manifest (plain ``*.py`` files and tests) is
# recognised by its source files; pytest needs no manifest to run.
_PYTHON_SOURCE_RULE: _Rule = ("*.py", "python", "", _PYTEST, "", "pip")

# Markers also searched below the root, up to this many directories deep
# (solution layouts keep their projects under src/ and tests/).
_NESTED_MARKERS = frozenset({"*.sln", "*.csproj", "*.fsproj"})
_NESTED_MARKER_DEPTH = 3

# Languages that share another language's toolchain (test runner, image).
_TOOLCHAIN = {"typescript": "javascript", "kotlin": "java", "fsharp": "csharp"}

# Source-file extensions per toolchain, to weigh languages by real code.
_SOURCE_EXTENSIONS: dict[str, frozenset[str]] = {
    "python": frozenset({".py"}),
    "javascript": frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}),
    "go": frozenset({".go"}),
    "rust": frozenset({".rs"}),
    "java": frozenset({".java", ".kt", ".kts"}),
    "ruby": frozenset({".rb"}),
    "php": frozenset({".php"}),
    "csharp": frozenset({".cs", ".fs"}),
    "powershell": frozenset({".ps1", ".psm1"}),
    "shell": frozenset({".sh", ".bash"}),
    "c++": frozenset({".cpp", ".cc", ".cxx", ".hpp", ".h"}),
    "c": frozenset({".c", ".h"}),
    "swift": frozenset({".swift"}),
    "dart": frozenset({".dart"}),
    "elixir": frozenset({".ex", ".exs"}),
    "scala": frozenset({".scala"}),
    "haskell": frozenset({".hs"}),
}

# Bounds of the workspace scan (nested markers, source counts).
_SCAN_DEPTH = 4
_SCAN_MAX_FILES = 10_000
_SCAN_SKIP_DIRS = frozenset(
    {"node_modules", "venv", "__pycache__", "bin", "obj", "build", "dist", "target", "vendor"}
)

# Framework detection from file contents or specific files
_FRAMEWORK_MARKERS: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("fastapi", "fastapi"),
        ("flask", "flask"),
        ("django", "django"),
        ("starlette", "starlette"),
        ("aiohttp", "aiohttp"),
        ("tornado", "tornado"),
        ("sanic", "sanic"),
    ],
    "javascript": [
        ("next.config", "nextjs"),
        ("nuxt.config", "nuxt"),
        ("svelte.config", "svelte"),
        ("astro.config", "astro"),
        ("vite.config", "vite"),
        ("angular.json", "angular"),
    ],
}


def detect_project(workspace_root: Path) -> ProjectInfo:
    """Detect project languages, frameworks, and tooling from file markers.

    One primary toolchain supplies the test and lint commands and the
    package manager: the only one whose markers matched or, when several
    matched, the one with the most source files (rule order breaks ties);
    it is ``languages[0]``.  A toolchain whose markers name no test command
    (a lone ``tsconfig.json``) is primary only when no other one can run
    tests.  Script and build helpers (``*.sh``, ``*.ps1``, ``Makefile``)
    count only when no manifest matched; then ``*.py`` files make a Python
    project too, so a repo of plain modules and tests gets pytest.  Another
    toolchain's languages are listed only when the workspace has source
    files in them (a ``package.json`` for formatting tools in a Python repo
    adds nothing).  Never raises on unreadable or oddly encoded manifests.
    """
    files = _scan_files(workspace_root)
    matched = _match_rules(workspace_root, files)
    primary_rules = [m for m in matched if m[0][0] not in _AUXILIARY_MARKERS]
    if not primary_rules:
        primary_rules = [*_python_sources(files), *matched]
    toolchains = list(dict.fromkeys(_toolchain(rule[1]) for rule, _ in primary_rules))
    counts = _source_counts(files) if len(toolchains) > 1 else Counter[str]()
    runnable = [
        t for t in toolchains if any(r[3] and _toolchain(r[1]) == t for r, _ in primary_rules)
    ]
    primary = max(runnable or toolchains, key=lambda t: counts[t], default="")
    kept = {t for t in toolchains if t == primary or counts[t] > 0}

    info = ProjectInfo()
    rules = sorted(
        (m for m in primary_rules if _toolchain(m[0][1]) in kept),
        key=lambda m: _toolchain(m[0][1]) != primary,
    )
    for (_marker, language, framework, test_cmd, lint_cmd, pkg_mgr), paths in rules:
        _append(info.languages, language)
        _append(info.frameworks, framework)
        info.config_files.extend(paths)
        if _toolchain(language) == primary:
            _append(info.test_commands, test_cmd)
            _append(info.lint_commands, lint_cmd)
            info.package_manager = info.package_manager or pkg_mgr

    # Deeper framework detection for Python (scan pyproject.toml/requirements.txt)
    if "python" in info.languages:
        _detect_python_frameworks(workspace_root, info)

    return info


def read_text_lenient(path: Path) -> str:
    """Read a text file in UTF-8 (BOM optional) or BOM-marked UTF-16, whatever the locale.

    Undecodable bytes become U+FFFD; an unreadable file reads as ``""``.
    PowerShell 5.1's ``pip freeze > requirements.txt`` writes UTF-16.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def _toolchain(language: str) -> str:
    return _TOOLCHAIN.get(language, language)


def _append(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _match_rules(root: Path, files: list[str]) -> list[tuple[_Rule, list[str]]]:
    """Return each rule whose marker exists, with the matching relative paths."""
    matched: list[tuple[_Rule, list[str]]] = []
    for rule in _DETECTION_RULES:
        marker = rule[0]
        if marker in _NESTED_MARKERS:
            paths = [
                f
                for f in files
                if f.count("/") <= _NESTED_MARKER_DEPTH
                and fnmatch.fnmatch(posixpath.basename(f), marker)
            ]
        elif marker.startswith("*"):
            paths = sorted(p.name for p in root.glob(marker) if p.is_file())
        else:
            paths = [marker] if (root / marker).exists() else []
        if paths:
            matched.append((rule, paths))
    return matched


def _scan_files(root: Path) -> list[str]:
    """List workspace files as sorted posix paths, bounded in depth and count.

    Hidden, dependency and build directories are skipped, and Delegatron's
    own directories at the root (``internal_dirs_for``: a user's ``runs/``
    package is scanned).
    """
    root_skip = _SCAN_SKIP_DIRS.union(internal_dirs_for(root))
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        depth = len(rel_dir.parts)
        skip = root_skip if depth == 0 else _SCAN_SKIP_DIRS
        dirnames[:] = [
            d for d in dirnames if depth < _SCAN_DEPTH and d not in skip and not d.startswith(".")
        ]
        files.extend((rel_dir / name).as_posix() for name in filenames)
        if len(files) >= _SCAN_MAX_FILES:
            break
    return sorted(files[:_SCAN_MAX_FILES])


def _python_sources(files: list[str]) -> list[tuple[_Rule, list[str]]]:
    """The Python source rule when the workspace has ``*.py`` files (no config files)."""
    return [(_PYTHON_SOURCE_RULE, [])] if _source_counts(files)["python"] else []


def _source_counts(files: list[str]) -> Counter[str]:
    """Count source files per toolchain."""
    counts: Counter[str] = Counter()
    for f in files:
        ext = posixpath.splitext(f)[1].lower()
        counts.update(t for t, exts in _SOURCE_EXTENSIONS.items() if ext in exts)
    return counts


def _detect_python_frameworks(root: Path, info: ProjectInfo) -> None:
    """Scan Python dependency files for framework markers."""
    content = "".join(
        read_text_lenient(root / name).lower() for name in ("pyproject.toml", "requirements.txt")
    )
    for marker, framework in _FRAMEWORK_MARKERS["python"]:
        if marker in content:
            _append(info.frameworks, framework)
