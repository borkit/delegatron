"""Project-level constraints — enforce rules on generated code.

Validates patches against user-defined constraints before applying them:
- no_modify: paths that must not be changed or deleted.  Matching is
  gitignore-style and case-sensitive: a trailing ``/`` protects a directory,
  a pattern without ``/`` also matches any path component (``.env`` protects
  ``config/.env``), and ``**/`` spans zero or more directories.
- required_patterns: text that at least one changed source file must contain
- forbidden_patterns: text that must not appear in the lines a patch adds
- guidelines: prose rules shown to the agents; never checked here

Pattern entries are literal substrings.  A ``re:`` prefix makes an entry a
regular expression, matched line by line for forbidden patterns.  Invalid
entries raise :class:`ConstraintConfigError` when the constraints are built.

Constraints come from the ``[constraints]`` table of delegatron.toml, run
templates, or the API request body.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from itertools import pairwise
from typing import Any

from delegatron.security.patch_lines import AddedLines, added_lines, patch_files
from delegatron.utils.paths import normalize_rel_path

logger = logging.getLogger(__name__)

REGEX_PREFIX = "re:"

_LIST_KEYS = ("no_modify", "guidelines")
_PATTERN_KEYS = ("required_patterns", "forbidden_patterns")


class ConstraintConfigError(ValueError):
    """A constraint entry is malformed (for example an invalid ``re:`` regex)."""


def compile_pattern(entry: str) -> re.Pattern[str]:
    """Compile one pattern entry: a literal substring, or a regex after ``re:``."""
    if not isinstance(entry, str):
        raise ConstraintConfigError(f"Constraint pattern must be a string, got {entry!r}")
    if not entry.startswith(REGEX_PREFIX):
        return re.compile(re.escape(entry))
    expression = entry[len(REGEX_PREFIX) :]
    if not expression:
        raise ConstraintConfigError(f"Empty regex in constraint pattern {entry!r}")
    try:
        return re.compile(expression)
    except re.error as exc:
        msg = f"Invalid regex in constraint pattern {entry!r}: {exc}"
        raise ConstraintConfigError(msg) from exc


@dataclass
class Constraints:
    """Project-level constraints for code generation.

    ``required_patterns`` and ``forbidden_patterns`` accept either:
    - ``dict[str, str]`` — ``{name: pattern}`` (from TOML config)
    - ``list[str]``       — ``[pattern, ...]``  (from API / dashboard / templates)

    Internally they are normalised to ``dict[str, str]``; a list entry is
    named after itself.  Patterns are compiled on construction.
    """

    no_modify: list[str] = field(default_factory=list)
    required_patterns: dict[str, str] = field(default_factory=dict)
    forbidden_patterns: dict[str, str] = field(default_factory=dict)
    guidelines: list[str] = field(default_factory=list)
    _required: dict[str, re.Pattern[str]] = field(init=False, repr=False, compare=False)
    _forbidden: dict[str, re.Pattern[str]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.no_modify = _string_list(self.no_modify, "no_modify")
        self.guidelines = _string_list(self.guidelines, "guidelines")
        self.required_patterns = _normalise_patterns(self.required_patterns, "required_patterns")
        self.forbidden_patterns = _normalise_patterns(self.forbidden_patterns, "forbidden_patterns")
        self._required = {n: compile_pattern(p) for n, p in self.required_patterns.items()}
        self._forbidden = {n: compile_pattern(p) for n, p in self.forbidden_patterns.items()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Constraints:
        return cls(
            no_modify=data.get("no_modify") or [],
            required_patterns=data.get("required_patterns") or {},
            forbidden_patterns=data.get("forbidden_patterns") or {},
            guidelines=data.get("guidelines") or [],
        )


def _string_list(value: Any, key: str) -> list[str]:
    """Validate a list of strings, dropping blank entries and duplicates."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConstraintConfigError(f"Constraint '{key}' must be a list of strings")
    return list(dict.fromkeys(v for v in value if v.strip()))


def _normalise_patterns(value: Any, key: str) -> dict[str, str]:
    """Convert a list of patterns or a ``{name: pattern}`` table to one dict.

    Blank entries are dropped; a pattern that is already present under
    another name is kept once.
    """
    if value is None:
        return {}
    if isinstance(value, list):
        items = [(v, v) for v in _string_list(value, key)]
    elif isinstance(value, Mapping):
        if not all(isinstance(v, str) for v in value.values()):
            raise ConstraintConfigError(f"Constraint '{key}' values must be strings")
        items = [(str(name), p) for name, p in value.items() if p.strip()]
    else:
        raise ConstraintConfigError(f"Constraint '{key}' must be a list or a table")
    patterns: dict[str, str] = {}
    for name, pattern in items:
        if pattern not in patterns.values():
            patterns[name] = pattern
    return patterns


def merge_constraints(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Union of two constraint dicts, e.g. delegatron.toml plus a run template.

    ``no_modify`` and ``guidelines`` are concatenated without duplicates;
    pattern lists and tables are normalised to ``{name: pattern}`` tables and
    merged, *extra* winning on a name conflict.  Other keys are copied, *extra*
    winning.  Empty known keys are dropped.

    Every pattern of the result is compiled, so a malformed entry (for
    example an invalid ``re:`` regex in a template) raises
    :class:`ConstraintConfigError` while the run is being configured rather
    than when the pipeline starts.
    """
    merged: dict[str, Any] = {**base, **extra}
    for key in _LIST_KEYS:
        values = [*_string_list(base.get(key), key), *_string_list(extra.get(key), key)]
        merged[key] = list(dict.fromkeys(values))
    for key in _PATTERN_KEYS:
        table = {
            **_normalise_patterns(base.get(key), key),
            **_normalise_patterns(extra.get(key), key),
        }
        merged[key] = _normalise_patterns(table, key)
        for pattern in merged[key].values():
            compile_pattern(pattern)
    return {k: v for k, v in merged.items() if v or k not in (*_LIST_KEYS, *_PATTERN_KEYS)}


@dataclass
class ConstraintViolation:
    """A single constraint violation.

    ``file`` is empty for a required pattern (it applies to the whole patch);
    ``line`` is set for forbidden patterns.
    """

    rule: str
    file: str
    detail: str
    line: int | None = None


def validate_patch(
    patch: Mapping[str, Any],
    constraints: Constraints,
    *,
    added: AddedLines | None = None,
) -> list[ConstraintViolation]:
    """Validate a patch against project constraints.

    *added* is the patch's :func:`~delegatron.security.patch_lines.added_lines`;
    pass it computed against the workspace so lines that already exist are not
    reported.  By default every line of the patch counts as added.

    Returns a list of violations. Empty list means the patch is valid.
    """
    lines = added if added is not None else added_lines(patch)
    violations = [
        *_no_modify_violations(patch, lines, constraints),
        *_required_violations(patch, lines, constraints),
        *_forbidden_violations(lines, constraints),
    ]

    if violations:
        logger.warning(
            "Patch violates %d constraint(s): %s",
            len(violations),
            ", ".join(v.rule for v in violations),
        )

    return violations


def _label(name: str, pattern: str) -> str:
    return repr(pattern) if name == pattern else f"'{name}' ({pattern!r})"


def _diff_source_paths(patch: Mapping[str, Any]) -> list[str]:
    """The ``---`` paths of the unified diff: the files it changes, renames or deletes.

    A deletion (``+++ /dev/null``) adds no lines, so its path is only here.
    """
    lines = str(patch.get("diff_unified") or "").splitlines()
    paths: list[str] = []
    for line, following in pairwise(lines):
        # A "---" line is a file header only right before its "+++" line.
        if line.startswith("--- ") and following.startswith("+++ "):
            source = line[4:].split("\t", 1)[0].strip()
            if source != "/dev/null":
                paths.append(source[2:] if source.startswith("a/") else source)
    return paths


def _no_modify_violations(
    patch: Mapping[str, Any], lines: AddedLines, constraints: Constraints
) -> list[ConstraintViolation]:
    removed = [*(patch.get("delete_files") or []), *_diff_source_paths(patch)]
    deleted = [normalize_rel_path(p) for p in removed]
    touched = dict.fromkeys([*lines, *(p for p in deleted if p)])
    violations: list[ConstraintViolation] = []
    for path in touched:
        pattern = next((p for p in constraints.no_modify if _matches_protected(path, p)), None)
        if pattern is not None:
            violations.append(
                ConstraintViolation(
                    rule="no_modify",
                    file=path,
                    detail=f"File matches no_modify pattern: {pattern}",
                )
            )
    return violations


def _required_violations(
    patch: Mapping[str, Any], lines: AddedLines, constraints: Constraints
) -> list[ConstraintViolation]:
    """One violation per required pattern that no changed source file contains."""
    files = patch_files(patch)
    sources = [
        files[path] if path in files else "\n".join(text for _, text in entries)
        for path, entries in lines.items()
        if _is_source_file(path)
    ]
    sources = [text for text in sources if text.strip()]
    if not sources:
        return []
    return [
        ConstraintViolation(
            rule="required_pattern",
            file="",
            detail=(
                f"Required pattern {_label(name, constraints.required_patterns[name])} "
                f"not found in any of the {len(sources)} changed source file(s)"
            ),
        )
        for name, regex in constraints._required.items()
        if not any(regex.search(text) for text in sources)
    ]


def _forbidden_violations(lines: AddedLines, constraints: Constraints) -> list[ConstraintViolation]:
    """One violation per forbidden pattern and file, at its first added line."""
    violations: list[ConstraintViolation] = []
    for path, entries in lines.items():
        for name, regex in constraints._forbidden.items():
            hit = next(((n, m) for n, text in entries if (m := regex.search(text))), None)
            if hit is not None:
                lineno, match = hit
                label = _label(name, constraints.forbidden_patterns[name])
                violations.append(
                    ConstraintViolation(
                        rule="forbidden_pattern",
                        file=path,
                        line=lineno,
                        detail=f"Forbidden pattern {label} found: {match.group()[:50]}",
                    )
                )
    return violations


def _posix(value: str) -> str:
    """Normalise a path or pattern: ``/`` separators, no leading ``./``."""
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _matches_protected(path: str, pattern: str) -> bool:
    """True when *path* is protected by the no_modify *pattern*.

    Like gitignore, a pattern that matches a directory protects everything
    inside it; a trailing ``/`` restricts the pattern to directories, a
    leading ``/`` anchors it to the workspace root, and a pattern without an
    inner ``/`` matches any single path component.
    """
    rel = _posix(path)
    pat = _posix(pattern)
    directory_only = pat.endswith("/")
    anchored = pat.startswith("/") or "/" in pat.strip("/")
    pat = pat.strip("/")
    if not rel or not pat:
        return False

    parts = rel.split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    if directory_only:
        candidates = candidates[:-1]
    if not anchored:
        return any(fnmatchcase(c.rsplit("/", 1)[-1], pat) for c in candidates)
    flat = pat.replace("**/", "")
    return any(fnmatchcase(c, pat) or fnmatchcase(c, flat) for c in candidates)


def _is_source_file(path: str) -> bool:
    """Check if a file path looks like a source code file."""
    extensions = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".cs",
        ".fs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
    }
    return any(path.endswith(ext) for ext in extensions)
