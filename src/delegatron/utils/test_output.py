"""Test-runner output parsing — per-test outcomes, failure excerpts, signatures.

The single shared parser for sandbox test output.  Sandboxes use it to fill
``SandboxReport.results`` / ``failed_tests`` / ``failure_excerpt``; the
orchestrator uses it for flaky-test tracking, stall detection and agent
context.  Pure functions, no I/O.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from typing import Any

# CSI escape sequences (colours, cursor movement) from --color=yes, jest, etc.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_STATUSES: dict[str, str] = {
    "PASSED": "passed",
    "FAILED": "failed",
    "ERROR": "error",
    "SKIPPED": "skipped",
    "XFAIL": "xfailed",
    "XPASS": "xpassed",
}
_STATUS = "(?P<status>" + "|".join(_STATUSES) + ")"

# Outcomes that count as a passing test result.
PASSING_OUTCOMES = frozenset({"passed", "skipped", "xfailed", "xpassed"})

# A test item ("path.py::Class::test_x[param]") or a bare module path, which
# pytest reports for collection errors ("ERROR tests/test_x.py").
_NODE_ID = r"(?P<nodeid>[^\s:]+(?:::[^\s:\[]+)+(?:\[.*?\])?|[^\s:]+\.py)"

# -v progress line: "tests/test_x.py::TestA::test_b PASSED    [ 50%]"
_VERBOSE_LINE = re.compile(rf"^{_NODE_ID}\s+{_STATUS}\b")

# Short test summary ("FAILED tests/test_x.py::test_b - AssertionError: ...")
# and the pytest-xdist progress line ("[gw0] [ 50%] PASSED tests/test_x.py::test_b").
_SUMMARY_LINE = re.compile(
    rf"^(?:\[gw\d+\]\s+\[\s*\d+%\]\s+)?{_STATUS}\s+{_NODE_ID}(?:\s+-\s.*)?\s*$"
)

_FAILURES_BANNER = re.compile(r"^=+ (?:FAILURES|ERRORS) =+$")
_SUMMARY_BANNER = re.compile(r"^=+ short test summary info =+$")
_ANY_BANNER = re.compile(r"^=+ .+ =+$")

# Per-failure section header: "_____ TestDeck.test_deal _____"
_SECTION_HEADER = re.compile(r"^_{3,} (?P<title>.+?) _{3,}$")
_SECTION_PREFIX = re.compile(r"^ERROR (?:collecting|at (?:setup|teardown) of) ")
_ERROR_LINE = re.compile(r"^E\s+(?P<text>\S.*)$")

# Failing test names from non-pytest runners: go test -v, jest, cargo test.
_OTHER_FAILURE = re.compile(
    r"^[ \t]*(?:--- FAIL: (\S+)|● (.+)|test (\S+) \.\.\. FAILED)",
    re.MULTILINE,
)

# A failed test (or subtest) in ``go test -v`` output: "--- FAIL: TestX (0.00s)".
_GO_FAILURE = re.compile(r"^[ \t]*--- FAIL: (\S+)", re.MULTILINE)

# Lines a compacted failure block keeps besides its header and "E" lines:
# the failing source line ("> ...") and the location ("tests/t.py:33: AssertionError").
_SOURCE_LINE = re.compile(r"^>")
_LOCATION_LINE = re.compile(r"^(?:[A-Za-z]:)?[^\s:][^:]*:\d+:(?:\s|$)")

# Memory addresses differ between runs of the same failure ("object at 0x7f3a...").
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")

# Literals masked in an error line so a failure keeps its signature while the
# values in it move ("12 != 10" and "14 != 12" are the same failure shape).
_DURATION = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:ms|us|µs|ns|s|sec|secs|seconds?)\b")
_QUOTED = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?!\w)")

_ELLIPSIS = "\n...\n"
_MAX_SIGNATURE_LINE = 200

# Longest line a compacted failure block keeps; longer ones end in _CUT.
_MAX_EXCERPT_LINE = 500
_CUT = "..."


def parse_pytest_outcomes(output: str) -> dict[str, str]:
    """Map each pytest node id to its outcome.

    Outcomes are ``passed``, ``failed``, ``error``, ``skipped``, ``xfailed``
    or ``xpassed``.  Reads ``-v`` progress lines and the short test summary;
    a later line for the same id wins (summary over progress, teardown ERROR
    over call PASSED).  Ids keep first-seen order.
    """
    outcomes: dict[str, str] = {}
    for line in _strip_ansi(output).splitlines():
        match = _VERBOSE_LINE.match(line) or _SUMMARY_LINE.match(line)
        if match:
            outcomes[match.group("nodeid")] = _STATUSES[match.group("status")]
    return outcomes


def build_results(output: str, cap: int = 1000) -> list[dict[str, Any]]:
    """Per-test ``SandboxReport.results`` entries parsed from pytest *output*.

    One ``{"name", "passed", "status", "error"}`` dict per node id, in
    first-seen order; ``passed`` covers passed, skipped, xfailed and
    xpassed, and a failing entry's ``error`` is the first ``E`` line of its
    failure block.  At most *cap* entries: failing ones are always kept,
    passing ones only while there is room.
    """
    outcomes = parse_pytest_outcomes(output)
    errors = _first_error_lines(_strip_ansi(output))
    failing = sum(status not in PASSING_OUTCOMES for status in outcomes.values())
    room = max(cap - failing, 0)
    results: list[dict[str, Any]] = []
    for nodeid, status in outcomes.items():
        passed = status in PASSING_OUTCOMES
        if passed:
            if room <= 0:
                continue
            room -= 1
        pending = None if passed else errors.get(_section_title(nodeid))
        error = pending.pop(0) if pending else ""
        results.append({"name": nodeid, "passed": passed, "status": status, "error": error})
    return results


def failed_test_ids(output: str) -> list[str]:
    """Return the ids of the tests that failed or errored, de-duplicated, in first-seen order.

    pytest node ids; for output without any pytest outcome, the test names
    of ``go test -v`` ``--- FAIL: TestX`` lines.  ``[]`` for other runners
    means "unknown", not "none failed".
    """
    outcomes = parse_pytest_outcomes(output)
    if outcomes:
        return [nodeid for nodeid, status in outcomes.items() if status in ("failed", "error")]
    return list(dict.fromkeys(_GO_FAILURE.findall(_strip_ansi(output))))


def format_test_failures(output: str, max_chars: int = 4000) -> str:
    """Return a readable failure excerpt of at most *max_chars* characters.

    For pytest, the text from the ``FAILURES`` / ``ERRORS`` banner through
    the short test summary and its closing result line.  When that is too
    long, each failure block is compacted to its header, its ``>`` source
    line, all of its ``E`` lines and its ``path:line:`` location (each line
    capped at ``_MAX_EXCERPT_LINE`` characters), followed by ``"\\n...\\n"``
    and the whole short test summary.  A block that still does not fit is
    shortened to its header and ``E`` lines, within its share of the room
    left, so every later block keeps its place.  A summary that would leave
    the blocks less than a quarter of the budget keeps only its tail.  Other
    runners (jest, go, cargo) get the tail of the output.
    """
    lines = _strip_ansi(output).splitlines()
    start = next((i for i, line in enumerate(lines) if _FAILURES_BANNER.match(line)), None)
    if start is None:
        return _tail("\n".join(lines).strip(), max_chars)

    end = len(lines)
    summary = next(
        (i for i in range(start, len(lines)) if _SUMMARY_BANNER.match(lines[i])),
        None,
    )
    if summary is not None:
        end = next(
            (i + 1 for i in range(summary + 1, len(lines)) if _ANY_BANNER.match(lines[i])),
            end,
        )
    excerpt = "\n".join(lines[start:end]).rstrip()
    if len(excerpt) <= max_chars:
        return excerpt

    budget = max_chars - len(_ELLIPSIS)
    if budget <= 0:
        return _tail(excerpt, max_chars)
    body_end = summary if summary is not None else end
    summary_text = "\n".join(lines[body_end:end]).rstrip()
    body = _compact_blocks(lines[start:body_end])
    if not summary_text:
        return _head(body, budget) + _ELLIPSIS.rstrip("\n")
    body_min = min(len("\n".join(body)), budget // 4)
    if len(summary_text) <= budget - body_min:
        return _head(body, budget - len(summary_text)) + _ELLIPSIS + summary_text
    return _head(body, body_min) + _ELLIPSIS + _tail(summary_text, budget - body_min)


def remove_failure_sections(
    output: str, node_ids: Iterable[str], *, keep: Iterable[str] = ()
) -> str:
    """*output* without the pytest failure sections and summary lines of *node_ids*.

    A failure section is pytest's ``____ title ____`` block (``ERROR
    collecting`` and ``ERROR at setup/teardown of`` ones included) up to the
    next section header or ``====`` banner.  It is removed when its title is
    that of an id in *node_ids* and of none in *keep*: titles leave out the
    file, so tests of two files can share one.  ``FAILED``/``ERROR`` short
    summary lines are removed for exactly the ids in *node_ids*.  Every other
    line is kept as it was, so output without such sections (or no ids)
    comes back unchanged; works on raw output and on
    :func:`format_test_failures` excerpts.
    """
    ids = set(node_ids)
    if not ids:
        return output
    drop_titles = {_section_title(nodeid) for nodeid in ids} - {
        _section_title(nodeid) for nodeid in keep
    }
    kept: list[str] = []
    skipping = False
    for line in output.splitlines(keepends=True):
        plain = _strip_ansi(line).rstrip("\r\n")
        header = _SECTION_HEADER.match(plain)
        if header or _ANY_BANNER.match(plain):
            title = _SECTION_PREFIX.sub("", header.group("title")) if header else None
            skipping = title in drop_titles
        if skipping:
            continue
        summary = _SUMMARY_LINE.match(plain)
        if (
            summary
            and summary.group("status") in ("FAILED", "ERROR")
            and summary.group("nodeid") in ids
        ):
            continue
        kept.append(line)
    return "".join(kept)


def _compact_blocks(lines: list[str]) -> list[str]:
    """Each failure block of a FAILURES/ERRORS section reduced to its key lines.

    A block starts at a banner or a ``_____ title _____`` header; it keeps
    that line, ``>`` source lines, ``E`` lines and ``path:line:`` locations,
    each cut to at most ``_MAX_EXCERPT_LINE`` characters.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if _ANY_BANNER.match(line) or _SECTION_HEADER.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [_cap_line(line.rstrip())]
        elif _ERROR_LINE.match(line) or _SOURCE_LINE.match(line) or _LOCATION_LINE.match(line):
            current.append(_cap_line(line.rstrip()))
    if current:
        blocks.append("\n".join(current))
    return blocks


def _cap_line(line: str) -> str:
    """*line*, cut to ``_MAX_EXCERPT_LINE`` characters ending in ``_CUT`` when longer."""
    if len(line) <= _MAX_EXCERPT_LINE:
        return line
    return line[: _MAX_EXCERPT_LINE - len(_CUT)] + _CUT


def _head(blocks: list[str], max_chars: int) -> str:
    """*blocks*, in order, within *max_chars* characters.

    A block that does not fit in the room left is shortened by
    :func:`_shorten_block` to its share of that room: what the later blocks
    leave over, and at least an equal split with them (and its header).  A
    block whose header does not fit is left out, and the later ones are
    still tried; only a first block that cannot keep its header is cut to
    the room.
    """
    kept: list[str] = []
    used = 0
    later = sum(len(block) + 1 for block in blocks)
    for index, block in enumerate(blocks):
        later -= len(block) + 1
        room = max_chars - used - (1 if kept else 0)
        if room <= 0:
            break
        if len(block) > room:
            header = len(block.split("\n", 1)[0])
            share = max(room - later, room // (len(blocks) - index), min(header, room))
            block = _shorten_block(block, share)
            if not block:
                if kept:
                    continue
                block = blocks[index][:room]
        kept.append(block)
        used += len(block) + (1 if len(kept) > 1 else 0)
    return "\n".join(kept)


def _shorten_block(block: str, max_chars: int) -> str:
    """A compacted failure *block* cut to at most *max_chars* characters.

    It keeps the header line and then the ``E`` lines that fit (the last of
    them cut, ending in ``_CUT``, when there is room for part of it); ``""``
    when the header alone does not fit.
    """
    header, *rest = block.split("\n")
    if len(header) > max_chars:
        return ""
    kept = [header]
    used = len(header)
    for line in rest:
        if not _ERROR_LINE.match(line):
            continue
        room = max_chars - used - 1
        if len(line) <= room:
            kept.append(line)
            used += len(line) + 1
            continue
        if room > 2 * len(_CUT):
            kept.append(line[: room - len(_CUT)] + _CUT)
        break
    return "\n".join(kept)


def failure_signature(report: dict[str, Any] | None) -> str:
    """Return a stable fingerprint of a failed test report.

    ``""`` for a missing or passing report.  Otherwise the sorted failing
    test ids (``report["failed_tests"]``, else parsed from the output, else
    the failing ``results`` names), each followed by its error: the
    ``results[].error`` recorded for it, else the first pytest ``E`` line
    of its failure; joined with ``"|"``.  Without ids, the failing
    go/jest/cargo test names, or else the last non-empty line of stderr
    (then stdout).

    Error lines keep only their shape: whitespace is collapsed and memory
    addresses, durations, quoted literals and numbers are masked, so a
    failure whose values move (``12 != 10``, then ``14 != 12``) keeps its
    signature while a different error does not.
    """
    if not report or report.get("passed"):
        return ""
    stdout = _strip_ansi(report.get("stdout") or "")
    stderr = _strip_ansi(report.get("stderr") or "")
    output = f"{stdout}\n{stderr}"

    result_errors = _failing_result_errors(report.get("results"))
    ids = list(
        dict.fromkeys(report.get("failed_tests") or failed_test_ids(output) or result_errors)
    )
    if ids:
        errors = _first_error_lines(output)
        parts = []
        for nodeid in ids:
            pending = errors.get(_section_title(nodeid))
            first_e_line = pending.pop(0) if pending else ""
            error = _error_shape(result_errors.get(nodeid) or first_e_line)
            parts.append(f"{nodeid} - {error}" if error else nodeid)
        return "|".join(sorted(parts))

    names = {_normalize(next(filter(None, m.groups()))) for m in _OTHER_FAILURE.finditer(output)}
    if names:
        return "|".join(sorted(names))

    for stream in (stderr, stdout):
        lines = [line for line in stream.splitlines() if line.strip()]
        if lines:
            return _error_shape(lines[-1])
    return ""


def extract_test_functions(source: str) -> set[str]:
    """Return the test functions defined in a Python test module.

    Top-level ``test*`` functions by name and ``test*`` methods as
    ``"ClassName::test_x"`` (nested classes chain with ``::``), sync or
    async.  Returns an empty set when *source* does not parse.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    return set(_iter_tests(tree.body, prefix=""))


def _iter_tests(body: list[ast.stmt], prefix: str) -> Iterator[str]:
    """Yield qualified test names from a module or class body."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                yield prefix + node.name
        elif isinstance(node, ast.ClassDef):
            yield from _iter_tests(node.body, f"{prefix}{node.name}::")


def _failing_result_errors(results: Any) -> dict[str, str]:
    """``{name: first error line}`` of the failing entries of a report's ``results``.

    Entries are dicts or :class:`SandboxTestResult`-like objects; passing
    ones are skipped, an entry without an error maps to ``""``.
    """
    errors: dict[str, str] = {}
    for result in results or []:
        entry = result if isinstance(result, dict) else getattr(result, "__dict__", {})
        name = entry.get("name")
        if not isinstance(name, str) or not name or entry.get("passed"):
            continue
        lines = [line for line in str(entry.get("error") or "").splitlines() if line.strip()]
        errors.setdefault(name, lines[0] if lines else "")
    return errors


def _first_error_lines(output: str) -> dict[str, list[str]]:
    """Map each failure-section title to the first ``E`` line of each such section."""
    errors: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        header = _SECTION_HEADER.match(line)
        if header:
            title = _SECTION_PREFIX.sub("", header.group("title"))
            current = errors.setdefault(title, [])
            current.append("")
            continue
        error = _ERROR_LINE.match(line)
        if error and current is not None and not current[-1]:
            current[-1] = _normalize(error.group("text"))
    return errors


def _section_title(nodeid: str) -> str:
    """Return the failure-section title pytest prints for *nodeid*.

    ``tests/t.py::TestA::test_b[1]`` -> ``TestA.test_b[1]``; a module id
    (collection error) is its own title.
    """
    _, sep, domain = nodeid.partition("::")
    return domain.replace("::", ".") if sep else nodeid


def _normalize(line: str) -> str:
    """Collapse whitespace, mask memory addresses and cap the length."""
    return _HEX_ADDRESS.sub("0x?", " ".join(line.split()))[:_MAX_SIGNATURE_LINE]


def _error_shape(line: str) -> str:
    """:func:`_normalize` *line*, also masking durations, quoted literals and numbers."""
    shape = _HEX_ADDRESS.sub("0x?", " ".join(line.split()))
    shape = _DURATION.sub("<dur>", shape)
    shape = _QUOTED.sub("<str>", shape)
    return _NUMBER.sub("<num>", shape)[:_MAX_SIGNATURE_LINE]


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return _ANSI.sub("", text)


def _tail(text: str, max_chars: int) -> str:
    """Return the last *max_chars* characters of *text* (``""`` when <= 0)."""
    return text[max(len(text) - max_chars, 0) :]
