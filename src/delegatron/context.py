"""Context management — selects relevant files to feed to the implementer.

Provides the implementer with actual file contents rather than just a
file tree, so it can generate accurate patches.  Files are chosen in this
priority order:

1. The current task's outputs
2. Failing test files, each followed by the local modules it imports
3. Files cited by review feedback (``file``, ``fix_file``, ``evidence``
   and ``recommended_fix``; locations and absolute sandbox roots are
   stripped and the path is matched against the workspace files), each
   followed by the local modules it imports
4. Files written by earlier tasks (``prior_files``)
5. Test <-> source counterparts of tiers 1-4 (``x.py`` <-> ``tests/test_x.py``
   / ``test_x.py`` / ``x_test.py``, root-level and ``src/`` layouts) and
   package ``__init__.py``
6. Files the task description names
7. Counterparts of tier 6, source files in the same directories, local
   modules imported by the files above or named in the task description,
   then the repository root's other source files (only when the root holds
   at most ``MAX_ROOT_SIBLINGS``)

Files of tiers 1-4 and 6 that are larger than what is left of the byte
budget are shown as a head + tail excerpt with an explicit
``... [truncated N bytes] ...`` marker (within such a tier the files that
fit whole are read smallest first); files of tiers 5 and 7 that do not fit
are left out.  Binary files are never decoded.  ``select_context`` reports
which files were excerpted, left out or skipped as binary, and logs each.

Also generates a structural "repo map" (class/function signatures)
using the ``repomap`` module, giving the LLM architectural awareness
of the entire project alongside the full content of relevant files.
"""

from __future__ import annotations

import ast
import logging
import posixpath
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from delegatron.repomap import DEFAULT_MAP_TOKENS, generate_repo_map, list_workspace_files
from delegatron.utils.paths import internal_dirs_for, normalize_rel_path
from delegatron.utils.test_output import failed_test_ids

logger = logging.getLogger(__name__)

# Largest share of the budget a single file may take (bytes).
MAX_FILE_SIZE = 50_000

# Max total context size (bytes) to avoid blowing up the prompt.
# Kept at 50KB to avoid overwhelming local models.
MAX_TOTAL_SIZE = 50_000

# Smallest excerpt worth showing (bytes); a file with less budget left is left out.
_MIN_EXCERPT_SIZE = 2_000

# A repository root with more source files than this is a flat collection of
# unrelated modules: its files are not added as siblings of a root-level target.
MAX_ROOT_SIBLINGS = 20

# Separates the head and tail of an excerpted file.
_TRUNCATION_MARKER = "\n... [truncated {} bytes] ...\n"
# Finds that marker in text, e.g. in a file an agent copied back from an excerpt.
TRUNCATION_MARKER_PATTERN = re.compile(
    re.escape(_TRUNCATION_MARKER.strip()).replace(re.escape("{}"), r"\d+")
)

# Review mode reads at most this many files, each capped at 1/N of the budget.
_REVIEW_MAX_FILES = 30
_REVIEW_FILE_SHARE = 8

# A NUL byte this close to the start of a file marks it as binary.
_BINARY_SNIFF_BYTES = 8192

# Suffixes treated as binary even when their first bytes hold no NUL.
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".db",
        ".sqlite",
        ".pyc",
        ".so",
        ".dll",
        ".exe",
        ".jar",
        ".class",
    }
)

# Extensions treated as source code (review-mode ranking, sibling expansion).
_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
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
        ".fs",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".swift",
        ".scala",
        ".ex",
        ".exs",
    }
)

# Top-level directories that hold tests rather than the modules under test.
_TEST_DIRS = frozenset({"tests", "test"})

# Review-mode ranking: package code first, helper directories late, tests last.
_PACKAGE_ROOTS = frozenset({"src", "lib", "app", "pkg", "internal", "cmd"})
_HELPER_ROOTS = frozenset({"benchmarks", "scripts", "examples", "docs", "bin", "tools"})
_VENDORED_SEGMENTS = frozenset({"node_modules", "venv", "__pycache__"})

# Test file names and the module they test: ``test_x.py``, ``x_test.go``,
# ``x.test.ts`` and ``x.spec.ts`` -> ``x.<ext>``.
_TEST_NAME_PREFIX = "test_"
_TEST_NAME_SUFFIXES = ("_test", ".test", ".spec")

# Punctuation wrapped around a path written in prose or markdown.
_PUNCTUATION = ",;:!?()[]{}<>\"'`*"

# A file path written in prose ("card_deck.py", "tests/test_x.py",
# "/workspace/tests/test_x.py", "C:/ws/x.py").  Hidden files (".env") never match.
_PATH_IN_TEXT = re.compile(r"(?:[A-Za-z]:)?/*(?:[\w.-]+/)*[\w-][\w.-]*\.\w+")

# Location suffix after a path: "x.py:15", "x.py:15:3" or "x.py::TestA::test_b".
_LOCATION_SUFFIX = re.compile(r"(?:::.*|(?::\d+)+)$")

# Import statements mentioned in prose ("... from app.models import User").
_IMPORT_IN_TEXT = re.compile(r"(?:from\s+|import\s+)([\w.]+)")

# Line-based import scan for Python files that do not parse.
_IMPORT_LINE = re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.MULTILINE)


class ContextSelection(BaseModel):
    """Files chosen for an agent prompt, and the relevant files it does not see whole.

    ``files`` maps each chosen path to its content, in priority order.  The
    other maps give the on-disk size in bytes of each relevant file that is
    shown only as a head + tail excerpt (``partial``), left out for lack of
    budget (``omitted``) or never decoded because it is binary (``binary``).
    """

    files: dict[str, str] = Field(default_factory=dict)
    partial: dict[str, int] = Field(default_factory=dict)
    omitted: dict[str, int] = Field(default_factory=dict)
    binary: dict[str, int] = Field(default_factory=dict)


class ReviewContext(ContextSelection):
    """Input for a read-only review of a whole codebase: ranked files plus the repo map."""

    repo_map: str = ""

    def source_dump(self) -> str:
        """The repo map, then each file, as one text block of ``--- ... ---`` sections."""
        sections = [f"\n--- Repository Structure ---\n{self.repo_map}\n"] if self.repo_map else []
        sections += [f"\n--- File: {path} ---\n{text}\n" for path, text in self.files.items()]
        return "".join(sections)


@dataclass(frozen=True)
class _FileBytes:
    """The bytes of a workspace file needed to show it whole or as an excerpt."""

    size: int
    head: bytes = b""
    tail: bytes = b""  # the last bytes, when the file is larger than the read window
    binary: bool = False


def select_context(
    workspace_root: Path,
    task: dict[str, Any] | None = None,
    review_feedback: list[dict[str, Any]] | None = None,
    file_tree: list[str] | None = None,
    max_total_size: int = MAX_TOTAL_SIZE,
    *,
    prior_files: list[str] | None = None,
    test_report: dict[str, Any] | None = None,
) -> ContextSelection:
    """Choose the workspace files the implementer should see.

    Returns at most ``max_total_size`` bytes of content in priority order
    (see the module docstring), plus the relevant files not shown whole.

    Args:
        workspace_root: Root of the workspace the paths are relative to.
        task: The current task (``outputs``, ``description``).
        review_feedback: Reviewer / validator issues for the current task.
        file_tree: Workspace file list.  Only a hint: when it is missing, or
            lacks a referenced file that exists on disk (a snapshot taken
            before earlier tasks wrote their files), the disk is walked.
        max_total_size: Byte budget for all file contents together.
        prior_files: Files written by completed tasks, dependencies first.
        test_report: The last sandbox report.  Files of its failing tests
            (``failed_tests``, else parsed from stdout/stderr) and the local
            modules they import are included.

    When it is given no task, feedback, prior file or test report (ad-hoc
    code review of an existing codebase), it is ``select_review_context``.
    Failing tests or prior files that match no workspace file select
    nothing rather than switching to review mode.
    """
    if not (task or review_feedback or prior_files or test_report):
        return select_review_context(workspace_root, file_tree, max_total_size)

    root = workspace_root.resolve()
    internal = internal_dirs_for(root)
    description = task.get("description", "") if task else ""
    outputs = _normalize_paths(task.get("outputs", []) if task else [], internal)
    mentioned = _normalize_paths(_path_tokens(description), internal)
    failing_tokens = _failing_test_tokens(test_report)
    cited_tokens = _feedback_tokens(review_feedback)
    prior = _normalize_paths(prior_files or [], internal)
    referenced = _normalize_paths([*failing_tokens, *cited_tokens], internal)
    tree = _resolve_file_tree(
        root, file_tree, [*outputs, *mentioned, *prior, *referenced], internal
    )
    failing = _canonical_refs(failing_tokens, root, tree, internal)
    cited = _canonical_refs(cited_tokens, root, tree, internal)

    targets = list(
        dict.fromkeys(
            [
                *outputs,
                *_with_local_imports(root, _existing(root, failing)),
                *_with_local_imports(root, _existing(root, cited)),
            ]
        )
    )
    earlier = [p for p in _existing(root, prior) if p not in targets]
    explicit = [*targets, *earlier]
    related = [p for p in _related_files(root, explicit, tree) if p not in explicit]
    seen = {*explicit, *related}
    described = [p for p in _existing(root, mentioned) if p not in seen]
    shown = [*explicit, *related, *described]
    imports = [*_text_import_targets(root, description), *_local_imports(root, shown)]
    candidates = [
        *_related_files(root, described, tree),
        *_sibling_files(shown, tree),
        *imports,
        *_root_siblings(shown, tree),
    ]
    rest = [p for p in dict.fromkeys(candidates) if p not in shown]
    tiers = [(targets, True), (earlier, True), (related, False), (described, True), (rest, False)]
    return _select_within_budget(root, tiers, max_total_size, MAX_FILE_SIZE)


def select_review_context(
    workspace_root: Path,
    file_tree: list[str] | None = None,
    max_total_size: int = MAX_TOTAL_SIZE,
) -> ContextSelection:
    """Choose the files for a read-only review of a whole codebase.

    Reads the top-ranked source files from ``select_review_files`` in rank
    order, each whole or excerpted to at most 1/8 of the budget.
    ``file_tree`` is a hint; an empty or missing one walks the disk.
    """
    root = workspace_root.resolve()
    tree = _resolve_file_tree(root, file_tree, [], internal_dirs_for(root))
    # One tier per file: strictly rank order, each whole or excerpted to its cap.
    review_tiers = [([p], True) for p in select_review_files(tree)]
    file_cap = max_total_size // _REVIEW_FILE_SHARE
    return _select_within_budget(root, review_tiers, max_total_size, file_cap)


def gather_review_context(
    workspace_root: Path,
    file_tree: list[str] | None = None,
    languages: list[str] | None = None,
    max_total_size: int = MAX_TOTAL_SIZE,
    map_tokens: int = DEFAULT_MAP_TOKENS,
) -> ReviewContext:
    """Everything a codebase review reads: the ranked files and the repo map.

    The one place the CLI ``review`` command and ``/api/review`` build their
    reviewer input from; ``ReviewContext.source_dump()`` renders it as text.
    """
    selection = select_review_context(workspace_root, file_tree, max_total_size)
    repo_map = gather_repo_map(
        workspace_root, file_tree=file_tree, languages=languages, max_tokens=map_tokens
    )
    return ReviewContext(**selection.model_dump(), repo_map=repo_map)


# What a focused codebase review asks the reviewer to look at (CLI and server).
REVIEW_FOCUS_INSTRUCTIONS: dict[str, str] = {
    "security": "Focus on security: injection, auth bypass, secrets, OWASP top 10.",
    "bugs": "Focus on bugs: edge cases, null handling, race conditions, error handling.",
    "performance": (
        "Focus on performance: N+1 queries, memory leaks, unnecessary computation, caching."
    ),
}


def review_budget_bytes(context_window: int) -> int:
    """Bytes of source a codebase review shows: half the reviewer's window, when known.

    *context_window* is in tokens (0 = unknown: ``MAX_TOTAL_SIZE``); the
    result never exceeds ``MAX_TOTAL_SIZE``.
    """
    from delegatron.utils.context_budget import CHARS_PER_TOKEN

    if context_window <= 0:
        return MAX_TOTAL_SIZE
    return max(1, min(MAX_TOTAL_SIZE, int(context_window * CHARS_PER_TOKEN) // 2))


def gather_context(
    workspace_root: Path,
    task: dict[str, Any] | None = None,
    review_feedback: list[dict[str, Any]] | None = None,
    file_tree: list[str] | None = None,
    max_total_size: int = MAX_TOTAL_SIZE,
    *,
    prior_files: list[str] | None = None,
    test_report: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Gather relevant file contents for the implementer.

    Returns ``{relative_posix_path: content}``; see ``select_context``.
    """
    return select_context(
        workspace_root,
        task,
        review_feedback,
        file_tree,
        max_total_size,
        prior_files=prior_files,
        test_report=test_report,
    ).files


def select_review_files(file_tree: Sequence[str], limit: int = _REVIEW_MAX_FILES) -> list[str]:
    """Rank source files for an ad-hoc review of a whole codebase.

    Package code (under ``src/``, ``lib/``, ``app/``, ... or a top-level
    Python package) comes first, then other source, then helper directories
    (``benchmarks/``, ``scripts/``, ``docs/``, ...); shallower and shorter
    paths first within a tier.  Tests are used only when there is no other
    source.  Returns at most *limit* posix paths.
    """
    paths = [
        p
        for p in file_tree
        if _is_source(p)
        and not any(part.startswith(".") or part in _VENDORED_SEGMENTS for part in p.split("/"))
    ]
    packages = {p.split("/")[0] for p in paths if p.count("/") == 1 and p.endswith("/__init__.py")}

    def tier(path: str) -> int:
        top = path.split("/")[0] if "/" in path else ""
        if top in _PACKAGE_ROOTS or top in packages:
            return 0
        return 2 if top in _HELPER_ROOTS else 1

    # Imported here: the orchestrator package imports this module.
    from delegatron.orchestrator.suite_guard import is_test_path

    code = [p for p in paths if not is_test_path(p)]
    ranked = sorted(code or paths, key=lambda p: (tier(p), p.count("/"), len(p)))
    return ranked[:limit]


def gather_repo_map(
    workspace_root: Path,
    task: dict[str, Any] | None = None,
    review_feedback: list[dict[str, Any]] | None = None,
    file_tree: list[str] | None = None,
    languages: list[str] | None = None,
    max_tokens: int = 1024,
    *,
    prior_files: list[str] | None = None,
) -> str:
    """Generate a structural repo map for the implementer.

    Returns a compact string showing class/function signatures across
    the project, ranked by relevance to the current task. This gives
    the LLM architectural awareness without consuming the full context
    budget.  Task outputs, files cited by review feedback and
    ``prior_files`` (written by earlier tasks) are ranked first.

    Returns an empty string if the project has no parseable source files.
    """
    root = workspace_root.resolve()
    internal = internal_dirs_for(root)
    # Build task context for relevance ranking
    task_context = ""
    mentioned: list[str] = []
    if task:
        task_context = task.get("description", "") + " " + task.get("title", "")
        mentioned.extend(_normalize_paths(task.get("outputs", []), internal))
    cited_tokens = _feedback_tokens(review_feedback)
    if cited_tokens:
        referenced = _normalize_paths(cited_tokens, internal)
        tree = _resolve_file_tree(root, file_tree, referenced, internal)
        mentioned.extend(_canonical_refs(cited_tokens, root, tree, internal))
    mentioned.extend(_normalize_paths(prior_files or [], internal))

    try:
        return generate_repo_map(
            workspace_root=root,
            task_context=task_context,
            mentioned_files=mentioned,
            max_tokens=max_tokens,
            languages=languages,
        )
    except Exception as e:
        logger.warning("Repo map generation failed: %s", e)
        return ""


def _normalize_paths(paths: Iterable[str], internal: Collection[str]) -> list[str]:
    """Return *paths* as workspace-relative posix paths, deduplicated.

    Absolute, drive, UNC and ``..`` paths are dropped before anything
    touches the disk, and so are paths in the workspace's *internal*
    directories (``internal_dirs_for``: ``_sandbox/``, Delegatron's ``runs/``...).
    """
    normalized: list[str] = []
    for path in paths:
        rel = normalize_rel_path(path)
        if rel is None:
            logger.debug("Ignoring unsafe path: %r", path)
        elif not _is_internal(rel, internal):
            normalized.append(rel)
    return list(dict.fromkeys(normalized))


def _is_internal(rel_path: str, internal: Collection[str]) -> bool:
    """True when the normalized *rel_path* lies in one of the *internal* root directories."""
    return rel_path.split("/", 1)[0] in internal


def _path_tokens(text: str) -> list[str]:
    """Return the file paths written in *text*, ``/``-separated, without location suffixes.

    Absolute paths are returned too; ``_normalize_paths`` drops them and
    ``_canonical_refs`` maps them onto workspace files.
    """
    paths: list[str] = []
    for word in text.split():
        token = word.replace("\\", "/").lstrip(_PUNCTUATION)
        token = _LOCATION_SUFFIX.sub("", token.rstrip(_PUNCTUATION + "."))
        token = token.rstrip(_PUNCTUATION + ".")
        if _PATH_IN_TEXT.fullmatch(token):
            paths.append(token)
    return list(dict.fromkeys(paths))


def _feedback_tokens(review_feedback: list[dict[str, Any]] | None) -> list[str]:
    """Return the paths review issues point at (``file``, ``fix_file``, evidence, fix text)."""
    paths: list[str] = []
    for issue in review_feedback or []:
        for key in ("file", "fix_file", "evidence", "recommended_fix"):
            value = issue.get(key)
            if isinstance(value, str):
                paths.extend(_path_tokens(value))
    return list(dict.fromkeys(paths))


def _failing_test_tokens(test_report: dict[str, Any] | None) -> list[str]:
    """Return the files of the failing tests in a sandbox report, first-seen order."""
    if not test_report:
        return []
    node_ids = test_report.get("failed_tests") or failed_test_ids(
        f"{test_report.get('stdout') or ''}\n{test_report.get('stderr') or ''}"
    )
    files = (i.split("::", 1)[0].replace("\\", "/") for i in node_ids if isinstance(i, str))
    return list(dict.fromkeys(files))


def _canonical_refs(
    tokens: Iterable[str], root: Path, tree: Sequence[str], internal: Collection[str]
) -> list[str]:
    """Map path tokens from agent output onto workspace files.

    A token that names an existing file is kept.  Otherwise the longest
    trailing part of it that names a file, or exactly one file in *tree*,
    is used, so absolute sandbox paths (``/workspace/tests/x.py``) and bare
    names (``card_deck.py`` for ``src/card_deck.py``) resolve.  Unmatched
    relative tokens are kept as written; unmatched absolute ones dropped,
    and so are paths in the *internal* directories.
    """
    known = set(tree)
    refs: list[str] = []
    for token in tokens:
        ref = _match_workspace_file(token, root, tree, known) or normalize_rel_path(token)
        if ref is not None and not _is_internal(ref, internal):
            refs.append(ref)
    return list(dict.fromkeys(refs))


def _match_workspace_file(
    token: str, root: Path, tree: Sequence[str], known: set[str]
) -> str | None:
    """Return the workspace file named by the longest trailing part of *token*."""
    parts = [p for p in token.split("/") if p not in ("", ".")]
    for start in range(len(parts)):
        suffix = "/".join(parts[start:])
        if ".." in parts[start:] or ":" in suffix:
            continue
        if suffix in known or _is_file(root, suffix):
            return suffix
        matches = [p for p in tree if p.endswith(f"/{suffix}")]
        if len(matches) == 1:
            return matches[0]
    return None


def _resolve_file_tree(
    root: Path, file_tree: list[str] | None, referenced: Iterable[str], internal: Collection[str]
) -> list[str]:
    """Return the workspace file list as posix paths.

    Uses ``file_tree`` when it is non-empty and knows every *referenced*
    path that exists on disk; otherwise walks the workspace.
    """
    tree = _normalize_paths(file_tree or [], internal)
    known = set(tree)
    if tree and not any(p not in known and _is_file(root, p) for p in referenced):
        return tree
    return list_workspace_files(root)


def _related_files(root: Path, paths: Iterable[str], tree: list[str]) -> list[str]:
    """Return existing test/source counterparts and package ``__init__.py`` of *paths*."""
    by_name: dict[str, list[str]] = {}
    for tree_path in tree:
        by_name.setdefault(posixpath.basename(tree_path), []).append(tree_path)

    related: list[str] = []
    for path in paths:
        p = PurePosixPath(path)
        module = _module_under_test(p.name)
        if module is not None:
            related.extend(_source_candidates(p, module, by_name))
        else:
            related.extend(_test_candidates(p, by_name))
        if p.parent.name:
            related.append(f"{p.parent}/__init__.py")
    return _existing(root, dict.fromkeys(related))


def _module_under_test(name: str) -> str | None:
    """The file name of the module a test file tests; None for a non-test name.

    ``test_x.py`` / ``x_test.py`` -> ``x.py``, ``x.test.ts`` / ``x.spec.ts`` -> ``x.ts``.
    """
    stem, ext = posixpath.splitext(name)
    if stem.startswith(_TEST_NAME_PREFIX) and len(stem) > len(_TEST_NAME_PREFIX):
        return stem.removeprefix(_TEST_NAME_PREFIX) + ext
    for suffix in _TEST_NAME_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem.removesuffix(suffix) + ext
    return None


def _test_candidates(source: PurePosixPath, by_name: dict[str, list[str]]) -> list[str]:
    """``x.py`` -> ``tests/test_x.py``, ``tests/**/test_x.py`` and ``test_x.py`` (root, beside)."""
    guess = _guess_test_path(source.as_posix())
    if guess is None:
        return []
    name = PurePosixPath(guess).name
    mirrored = [p for p in by_name.get(name, []) if p.startswith("tests/")]
    return [guess, name, (source.parent / name).as_posix(), *mirrored]


def _source_candidates(test: PurePosixPath, name: str, by_name: dict[str, list[str]]) -> list[str]:
    """Where the module *name* a test file tests may live.

    ``x.py``, ``src/x.py``, beside the test, ``<pkg>/x.py`` and ``src/<pkg>/x.py``.
    """
    packaged = [
        p
        for p in by_name.get(name, [])
        if (p.count("/") == 1 and p.split("/")[0] not in _TEST_DIRS)
        or (p.count("/") == 2 and p.startswith("src/"))
    ]
    return [name, f"src/{name}", (test.parent / name).as_posix(), *packaged]


def _sibling_files(paths: Iterable[str], tree: list[str]) -> list[str]:
    """Return source files from *tree* in the (non-root) directories of *paths*.

    This helps the implementer see neighbouring modules so it can write
    code that integrates well (e.g. consistent patterns, shared helpers).
    """
    dirs = {posixpath.dirname(p) for p in paths} - {""}
    return [p for p in tree if posixpath.dirname(p) in dirs and _is_source(p)]


def _root_siblings(paths: Iterable[str], tree: list[str]) -> list[str]:
    """Root-level source files of *tree* when a path in *paths* is at the root.

    None when the root holds more than ``MAX_ROOT_SIBLINGS`` source files:
    a flat repository's root modules are mostly unrelated to the target.
    """
    if all("/" in p for p in paths):
        return []
    root_sources = [p for p in tree if "/" not in p and _is_source(p)]
    return root_sources if len(root_sources) <= MAX_ROOT_SIBLINGS else []


def _text_import_targets(root: Path, text: str) -> list[str]:
    """Map import statements mentioned in prose to existing workspace modules."""
    modules = [m.strip(".") for m in _IMPORT_IN_TEXT.findall(text)]
    candidates = [
        path
        for module in modules
        if module
        for path in _module_paths(module, [PurePosixPath(), PurePosixPath("src")])
    ]
    return _existing(root, dict.fromkeys(candidates))


def _with_local_imports(root: Path, paths: Iterable[str]) -> list[str]:
    """Return each of *paths* followed by the workspace modules it imports."""
    ordered: list[str] = []
    for path in paths:
        ordered += [path, *_local_imports(root, [path])]
    return list(dict.fromkeys(ordered))


def _local_imports(root: Path, paths: Iterable[str]) -> list[str]:
    """Return the existing workspace modules imported by the Python files in *paths*."""
    found: list[str] = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        source = _read_text(root, path)
        if source is not None:
            package = PurePosixPath(path).parent
            found += [p for p in _import_candidates(source, package) if p != path]
    return _existing(root, dict.fromkeys(found))


def _import_candidates(source: str, package: PurePosixPath) -> list[str]:
    """Return the paths a Python module's imports may resolve to (unchecked).

    Absolute imports are tried from the root, ``src/`` and the module's own
    directory (pytest puts a test's rootdir on ``sys.path``); relative
    imports from the package they climb to.  ``from m import n`` also tries
    the submodule ``m.n``.
    """
    candidates: list[str] = []
    for level, module, names in _parse_imports(source):
        if level:
            base = package
            for _ in range(level - 1):
                base = base.parent
            bases = [base]
        else:
            bases = [PurePosixPath(), PurePosixPath("src"), package]
        prefix = f"{module}." if module else ""
        modules = [module] if module else []
        modules += [prefix + name for name in names]
        for dotted in modules:
            candidates += _module_paths(dotted, bases)
    return candidates


def _parse_imports(source: str) -> list[tuple[int, str, list[str]]]:
    """Return ``(level, module, imported names)`` for each import in *source*.

    Falls back to a line scan (absolute module names only) when the source
    does not parse, which is common for generated tests being fixed.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        modules = (m.strip(".") for m in _IMPORT_LINE.findall(source))
        return [(0, module, []) for module in modules if module]
    imports: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [(0, alias.name, []) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names if alias.name != "*"]
            imports.append((node.level, node.module or "", names))
    return imports


def _module_paths(module: str, bases: Iterable[PurePosixPath]) -> list[str]:
    """Return ``<base>/<module>.py`` and ``<base>/<module>/__init__.py`` for each base."""
    paths: list[str] = []
    for base in bases:
        stem = base.joinpath(*module.split("."))
        paths += [f"{stem}.py", f"{stem}/__init__.py"]
    return paths


def _guess_test_path(source_path: str) -> str | None:
    """Guess the test file path for a source file."""
    # src/app/health.py -> tests/test_health.py
    p = Path(source_path)
    filename = p.name
    if filename.startswith("test_") or filename.startswith("__"):
        return None  # Already a test file or special file (__init__.py, etc.)
    test_name = f"test_{filename}"
    return f"tests/{test_name}"


def _is_source(path: str) -> bool:
    """Return True for source-code files (binary/data files are skipped)."""
    return posixpath.splitext(path)[1].lower() in _SOURCE_EXTENSIONS


def _is_file(root: Path, rel_path: str) -> bool:
    """Return True if *rel_path* is an existing file under *root*."""
    try:
        return (root / rel_path).is_file()
    except OSError:
        return False


def _existing(root: Path, paths: Iterable[str]) -> list[str]:
    """Return the *paths* that exist as files under *root*, order kept."""
    return [p for p in paths if _is_file(root, p)]


def _workspace_file(root: Path, rel_path: str) -> Path | None:
    """Resolve a workspace file; None if it is missing or (via symlinks) outside *root*."""
    full_path = (root / rel_path).resolve()
    if not full_path.is_relative_to(root):
        logger.warning("Path traversal blocked: %s", rel_path)
        return None
    return full_path if full_path.is_file() else None


def _read_text(root: Path, rel_path: str) -> str | None:
    """Read a workspace file; None if it is missing, unreadable or outside *root*."""
    try:
        full_path = _workspace_file(root, rel_path)
        return None if full_path is None else _decode(full_path.read_bytes())
    except OSError as e:
        logger.debug("Could not read %s: %s", rel_path, e)
        return None


def _load(root: Path, rel_path: str, window: int) -> _FileBytes | None:
    """Read up to *window* bytes from each end of a workspace file.

    Returns None if it is missing, unreadable or outside *root*.  Binary
    files (a known suffix, or a NUL byte near the start) are flagged and
    their content is not kept.
    """
    try:
        full_path = _workspace_file(root, rel_path)
        if full_path is None:
            return None
        size = full_path.stat().st_size
        if full_path.suffix.lower() in _BINARY_SUFFIXES:
            return _FileBytes(size, binary=True)
        with full_path.open("rb") as f:
            head = f.read(window)
            tail = b""
            if size > window:
                f.seek(max(window, size - window))
                tail = f.read()
    except OSError as e:
        logger.debug("Could not read %s: %s", rel_path, e)
        return None
    if b"\0" in head[:_BINARY_SNIFF_BYTES]:
        return _FileBytes(size, binary=True)
    return _FileBytes(size, head, tail)


def _decode(data: bytes) -> str:
    """Decode file bytes as UTF-8 regardless of the host locale, with ``\\n`` line ends."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _select_within_budget(
    root: Path,
    tiers: Sequence[tuple[Sequence[str], bool]],
    max_total_size: int,
    file_cap: int,
) -> ContextSelection:
    """Read *tiers* of ``(paths, may_excerpt)`` within *max_total_size* bytes.

    Within a tier, every file that fits whole (and is at most *file_cap*
    bytes) is read first, smallest first in a *may_excerpt* tier so one
    large file cannot crowd out the others, in path order otherwise.  Then
    each remaining file of a *may_excerpt* tier is shown as an excerpt of
    what is left of the budget (at most *file_cap*); otherwise, or when
    less than ``_MIN_EXCERPT_SIZE`` is left, it is left out.  Files are
    returned in tier order.  Each excerpted, left-out or binary file is
    logged with its size.
    """
    chosen: dict[str, str] = {}
    partial: dict[str, int] = {}
    omitted: dict[str, int] = {}
    binary: dict[str, int] = {}
    remaining = max_total_size
    window = max(file_cap, _BINARY_SNIFF_BYTES)

    for paths, may_excerpt in tiers:
        loaded: list[tuple[str, _FileBytes]] = []
        for rel_path in paths:
            data = _load(root, rel_path, window)
            if data is None:
                continue
            if data.binary:
                binary[rel_path] = data.size
                logger.info("Context: skipped binary file %s (%d bytes)", rel_path, data.size)
            else:
                loaded.append((rel_path, data))
        # sorted() is stable: equal sizes keep their priority order.
        whole_first = sorted(loaded, key=lambda item: item[1].size) if may_excerpt else loaded
        oversized: set[str] = set()
        for rel_path, data in whole_first:
            text = "" if data.tail else _decode(data.head)
            size = len(text.encode())
            if not data.tail and size <= min(file_cap, remaining):
                chosen[rel_path] = text
                remaining -= size
            elif may_excerpt:
                oversized.add(rel_path)
            else:
                _leave_out(omitted, rel_path, data.size, remaining)
        for rel_path, data in loaded:  # excerpts in priority order
            if rel_path not in oversized:
                continue
            cap = min(file_cap, remaining)
            if cap < _MIN_EXCERPT_SIZE:
                _leave_out(omitted, rel_path, data.size, remaining)
                continue
            chosen[rel_path] = _excerpt(data, cap)
            partial[rel_path] = data.size
            shown = len(chosen[rel_path].encode())
            remaining -= shown
            logger.info(
                "Context: %s (%d bytes) shown as a %d-byte excerpt", rel_path, data.size, shown
            )

    files = {p: chosen[p] for paths, _ in tiers for p in paths if p in chosen}
    logger.info("Context: gathered %d files (%d bytes)", len(files), max_total_size - remaining)
    if partial or omitted or binary:
        logger.info(
            "Context: %d excerpted, %d left out over the %d-byte budget, %d binary skipped",
            len(partial),
            len(omitted),
            max_total_size,
            len(binary),
        )
    return ContextSelection(files=files, partial=partial, omitted=omitted, binary=binary)


def _leave_out(omitted: dict[str, int], rel_path: str, size: int, remaining: int) -> None:
    """Record and log a relevant file that is not shown for lack of budget."""
    omitted[rel_path] = size
    logger.info(
        "Context: left out %s (%d bytes, %d bytes of budget left)", rel_path, size, remaining
    )


def _excerpt(data: _FileBytes, cap: int) -> str:
    """Show the first and last lines of a file in at most *cap* bytes, marking the cut."""
    room = cap - len(_TRUNCATION_MARKER.format(data.size))
    head = _leading_lines(_decode(data.head), room * 2 // 3)
    tail = _trailing_lines(_decode(data.tail or data.head), room - room * 2 // 3)
    cut = max(data.size - len(head.encode()) - len(tail.encode()), 0)
    return head + _TRUNCATION_MARKER.format(cut) + tail


def _leading_lines(text: str, limit: int) -> str:
    """The longest prefix of *text* within *limit* UTF-8 bytes, ending after a newline if any."""
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    end = encoded.rfind(b"\n", 0, limit) + 1 or limit
    return encoded[:end].decode(errors="ignore")


def _trailing_lines(text: str, limit: int) -> str:
    """The longest suffix of *text* within *limit* UTF-8 bytes, starting a line if possible."""
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    start = len(encoded) - limit
    if encoded[start - 1 : start] != b"\n":
        start = encoded.find(b"\n", start, len(encoded) - 1) + 1 or start
    return encoded[start:].decode(errors="ignore")
