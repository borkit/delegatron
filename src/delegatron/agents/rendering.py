"""Markdown building blocks for the agents' user messages.

Agents render structured input as markdown sections.  Code, files and test
output go into fenced blocks as real text (never JSON-escaped), so a model
reads a traceback or a source file exactly as a developer would.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from delegatron.utils.test_output import format_test_failures

_BACKTICK_RUN = re.compile(r"`{3,}")


def fenced(text: str, lang: str = "") -> str:
    """Wrap *text* in a code fence that no backtick run inside it can close."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    body = text.rstrip("\n")
    return f"{fence}{lang}\n{body}\n{fence}"


def json_block(value: Any) -> str:
    """Render *value* as an indented JSON code block."""
    return fenced(json.dumps(value, indent=2, ensure_ascii=False), "json")


def files_block(files: Mapping[str, str], partial: Mapping[str, int] | None = None) -> str:
    """Render ``{path: content}`` as a ``### path`` heading and code block per file.

    A file in *partial* (``{path: on-disk bytes}``) is shown only as an
    excerpt, and its heading says so: ``### path (PARTIAL: N of M bytes shown)``.
    """
    partial = partial or {}

    def heading(path: str, content: str) -> str:
        size = partial.get(path)
        if size is None:
            return path
        return f"{path} (PARTIAL: {len(content.encode('utf-8'))} of {size} bytes shown)"

    return "\n\n".join(
        f"### {heading(path, content)}\n{fenced(content)}" for path, content in files.items()
    )


def bullet_list(items: Iterable[str]) -> str:
    """Render *items* as a markdown bullet list."""
    return "\n".join(f"- {item}" for item in items)


def failure_list(failures: Iterable[str] | Mapping[str, Any]) -> str:
    """Render test ids as a bullet list; ``{id: origin}`` adds the origin in parentheses."""
    if isinstance(failures, Mapping):
        return bullet_list(
            f"{test} ({origin})" if origin else str(test) for test, origin in failures.items()
        )
    return bullet_list(failures)


def result_line(report: Mapping[str, Any]) -> str:
    """``Result: PASSED|FAILED (N failed of M)`` for a test report or its counts.

    A skipped run (``skipped_reason``) is ``Result: SKIPPED (no tests ran)``:
    neither a pass nor a failure.
    """
    if report.get("skipped_reason"):
        return "Result: SKIPPED (no tests ran)"
    verdict = "PASSED" if report.get("passed") else "FAILED"
    return f"Result: {verdict} ({report.get('failures', 0)} failed of {report.get('total', 0)})"


def removed_tests_section(removed_tests: Iterable[Mapping[str, Any]]) -> str:
    """The "Removed Tests" section: ``{"file", "removed"}`` items as ``file: names``.

    An item's ``owner_task`` (the task whose accepted tests they were) is named.
    """
    lines = (
        f"{item['file']}: {', '.join(item['removed'])}"
        + (f" (accepted in task {item['owner_task']})" if item.get("owner_task") else "")
        for item in removed_tests
    )
    return "## Removed Tests\n\n" + bullet_list(lines)


def report_excerpt(report: Mapping[str, Any]) -> str:
    """Return the readable output of a test report.

    The sandbox's ``failure_excerpt`` when present (computed from the full
    output before truncation), else :func:`format_test_failures` of the
    report's stdout and stderr (older reports, early sandbox exits).
    """
    excerpt = report.get("failure_excerpt") or ""
    if excerpt:
        return str(excerpt)
    output = "\n".join(part for part in (report.get("stdout"), report.get("stderr")) if part)
    return format_test_failures(output)
