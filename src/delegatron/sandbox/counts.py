"""Test counts (total, failures) from a test runner's output, for every sandbox.

One parser for the Docker, local and E2B sandboxes.  Each runner is read
only from its own summary lines, so a line one runner prints is never
counted by another's pattern: pytest only from its closing result line
(``== 2 failed, 5 passed in 0.12s ==``), never from text such as
``Interrupted: 1 error during collection`` or an assertion message that
happens to contain "2 errors".  Counts from several runners in one output
(several test commands) are added up.  ``(0, 0)`` means nothing was
recognised.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# pytest's closing line, with or without the "=" banner and the duration.
_PYTEST_SUMMARY = re.compile(
    r"^=*\s*(?P<counts>\d+ [a-z]+(?:, \d+ [a-z]+)*)"
    r"(?: in [\d.]+s(?: \([^)]*\))?)?\s*=*\s*$"
)
_PYTEST_COUNT = re.compile(r"(\d+) ([a-z]+)")
_PYTEST_WORDS = frozenset(
    {
        "passed",
        "failed",
        "error",
        "errors",
        "skipped",
        "xfailed",
        "xpassed",
        "deselected",
        "warning",
        "warnings",
        "rerun",
        "reruns",
    }
)
_PYTEST_RAN = frozenset({"passed", "failed", "error", "errors", "xfailed", "xpassed"})
_PYTEST_FAILED = frozenset({"failed", "error", "errors"})

_FAILED_COUNT = re.compile(r"(\d+) failed")
# jest/vitest lines that count suites or files, not tests
_TEST_FILES_LINE = re.compile(r"^\s*Test (?:Suites|Files)\s")
# jest: "Tests:       2 failed, 5 passed, 7 total"
_JEST = re.compile(r"^\s*Tests:\s+(?P<body>.*?)(?P<total>\d+) total")
# vitest: "      Tests  1 failed | 1 passed (2)"
_VITEST = re.compile(r"^\s*Tests\s+(?P<body>.+?)\s+\((?P<total>\d+)\)\s*$")
# Maven/Gradle surefire summary (per-class lines end in "-- in <Class>" and are skipped)
_SUREFIRE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)(?:,\s*Skipped:\s*\d+)?\s*$"
)
# PHPUnit: "OK (4 tests, 4 assertions)" / "Tests: 7, Assertions: 15, Errors: 1, Failures: 2."
_PHPUNIT_OK = re.compile(r"^\s*OK\s+\((\d+)\s+tests?,")
_PHPUNIT = re.compile(r"^\s*Tests:\s*(\d+),\s*Assertions:\s*\d+")
_PHPUNIT_FAILURES = re.compile(r"\b(?:Failures|Errors):\s*(\d+)")
# cargo: "test result: FAILED. 2 passed; 3 failed; ..." (one per test binary)
_CARGO = re.compile(r"test result:.*?(\d+)\s+passed;\s+(\d+)\s+failed")
# dotnet: "Failed!  - Failed: 1, Passed: 11, Skipped: 0, Total: 12, ..." (fields in any
# order), .NET 9 "Test summary: total: 12, failed: 1, ...", vstest "Total tests: 12"
# followed by indented "Passed: 11" / "Failed: 1" lines
_DOTNET_TOTAL = re.compile(r"\bTotal:\s*(\d+)")
_DOTNET_FAILED = re.compile(r"\bFailed:\s*(\d+)")
_DOTNET_SUMMARY = re.compile(r"^\s*Test summary:\s*total:\s*(\d+),\s*failed:\s*(\d+)", re.I)
_VSTEST_TOTAL = re.compile(r"^\s*Total tests:\s*(\d+)\s*$")
_VSTEST_FIELD = re.compile(r"^\s+(?P<name>[A-Za-z][A-Za-z ]*):\s*(?P<value>\S+)")
# go test -v per test, and the per-package result lines
_GO_TEST = re.compile(r"^---\s+(PASS|FAIL):")
_GO_PACKAGE = re.compile(r"^(ok|FAIL)\s+\S+\s+(?:[\d.]+s|\(cached\))")
# mocha: "  3 passing (12ms)" / "  1 failing"
_MOCHA_PASSING = re.compile(r"^\s*(\d+) passing\b")
_MOCHA_FAILING = re.compile(r"^\s*(\d+) failing\b")
# rspec: "5 examples, 1 failure"
_RSPEC = re.compile(r"^(\d+) examples?, (\d+) failures?\b")
# node:test (TAP "# tests 5" / spec reporter "<info sign> tests 5")
_NODE_TESTS = re.compile(r"^(?:#|\u2139) tests (\d+)\s*$")
_NODE_FAIL = re.compile(r"^(?:#|\u2139) fail (\d+)\s*$")
# unittest / Django: "Ran 5 tests in 0.01s", then "OK" or "FAILED (failures=1, errors=1)"
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ")
_UNITTEST_FAILED = re.compile(r"^FAILED \((?P<body>[^)]*)\)\s*$")
_UNITTEST_COUNT = re.compile(r"\b(?:failures|errors)=(\d+)")
# ctest: "75% tests passed, 1 tests failed out of 4"
_CTEST = re.compile(r"tests passed, (\d+) tests? failed out of (\d+)")
# Pester: "Tests Passed: 4, Failed: 1, Skipped: 0 NotRun: 0"
_PESTER = re.compile(r"Tests Passed:\s*(\d+),\s*Failed:\s*(\d+)")
# Gradle on failure: "4 tests completed, 1 failed"
_GRADLE = re.compile(r"^(\d+) tests? completed, (\d+) failed")

_Match = re.Match[str]


@dataclass
class _Tally:
    total: int = 0
    failures: int = 0

    def add(self, counts: tuple[int, int]) -> None:
        self.total += counts[0]
        self.failures += counts[1]


def parse_test_counts(output: str) -> tuple[int, int]:
    """Return ``(total, failures)`` for the tests in *output*; ``(0, 0)`` when unknown.

    *total* counts tests that ran (passed, failed or errored; skipped ones
    are left out) and *failures* those that failed or errored.
    """
    tally = _Tally()
    go_tests = _Tally()
    go_packages = _Tally()
    unittest_ran = False
    vstest_block = False
    for line in _ANSI.sub("", output).splitlines():
        line = line.rstrip("\r")
        if vstest_block:
            field = _VSTEST_FIELD.match(line)
            if field:
                if field.group("name") == "Failed" and field.group("value").isdigit():
                    tally.add((0, int(field.group("value"))))
                continue
            vstest_block = False
        if go := _GO_TEST.match(line):
            go_tests.add((1, int(go.group(1) == "FAIL")))
        elif package := _GO_PACKAGE.match(line):
            go_packages.add((1, int(package.group(1) == "FAIL")))
        elif (counts := _line_counts(line)) is not None:
            tally.add(counts)
        elif ran := _UNITTEST_RAN.match(line):
            tally.add((int(ran.group(1)), 0))
            unittest_ran = True
        elif unittest_ran and (failed := _UNITTEST_FAILED.match(line)):
            tally.add((0, sum(int(n) for n in _UNITTEST_COUNT.findall(failed.group("body")))))
            unittest_ran = False
        elif vstest := _VSTEST_TOTAL.match(line):
            tally.add((int(vstest.group(1)), 0))
            vstest_block = True
    # go -v prints both per-test lines and the package line: per-test wins.
    go_tally = go_tests if go_tests.total else go_packages
    return tally.total + go_tally.total, tally.failures + go_tally.failures


def _line_counts(line: str) -> tuple[int, int] | None:
    """The counts one summary line reports, or ``None`` when it is no summary line."""
    counts = _pytest_counts(line)
    if counts is not None:
        return counts
    for matcher, reader in _LINE_READERS:
        match = matcher(line)
        if match:
            return reader(match, line)
    return None


def _pytest_counts(line: str) -> tuple[int, int] | None:
    match = _PYTEST_SUMMARY.match(line)
    if not match:
        return None
    pairs = [(int(n), word) for n, word in _PYTEST_COUNT.findall(match.group("counts"))]
    if not pairs or any(word not in _PYTEST_WORDS for _, word in pairs):
        return None
    total = sum(n for n, word in pairs if word in _PYTEST_RAN)
    failures = sum(n for n, word in pairs if word in _PYTEST_FAILED)
    return total, failures


def _dotnet_line(line: str) -> _Match | None:
    """A dotnet result line names both ``Total:`` and ``Failed:`` (in any order)."""
    return _DOTNET_TOTAL.search(line) if _DOTNET_FAILED.search(line) else None


def _failed_in_body(match: _Match, _line: str) -> tuple[int, int]:
    failed = _FAILED_COUNT.search(match.group("body"))
    return int(match.group("total")), int(failed.group(1)) if failed else 0


def _surefire(match: _Match, line: str) -> tuple[int, int]:
    if " -- in " in line or " - in " in line:
        return 0, 0
    return int(match.group(1)), int(match.group(2)) + int(match.group(3))


def _phpunit(match: _Match, line: str) -> tuple[int, int]:
    return int(match.group(1)), sum(int(n) for n in _PHPUNIT_FAILURES.findall(line))


def _dotnet(match: _Match, line: str) -> tuple[int, int]:
    failed = _DOTNET_FAILED.search(line)
    return int(match.group(1)), int(failed.group(1)) if failed else 0


def _total_failures(match: _Match, _line: str) -> tuple[int, int]:
    """Groups ``(total, failures)``."""
    return int(match.group(1)), int(match.group(2))


def _passed_failed(match: _Match, _line: str) -> tuple[int, int]:
    """Groups ``(passed, failed)``."""
    return int(match.group(1)) + int(match.group(2)), int(match.group(2))


def _failed_of_total(match: _Match, _line: str) -> tuple[int, int]:
    """Groups ``(failed, total)``."""
    return int(match.group(2)), int(match.group(1))


def _total_only(match: _Match, _line: str) -> tuple[int, int]:
    return int(match.group(1)), 0


def _failed_tests(match: _Match, _line: str) -> tuple[int, int]:
    """Failed tests that are not counted anywhere else on their own (mocha)."""
    return int(match.group(1)), int(match.group(1))


def _failures_only(match: _Match, _line: str) -> tuple[int, int]:
    """A failure count for tests a total line already counted (node:test)."""
    return 0, int(match.group(1))


def _no_tests(_match: _Match, _line: str) -> tuple[int, int]:
    return 0, 0


_LINE_READERS: tuple[
    tuple[Callable[[str], _Match | None], Callable[[_Match, str], tuple[int, int]]], ...
] = (
    (_TEST_FILES_LINE.match, _no_tests),
    (_JEST.search, _failed_in_body),
    (_VITEST.search, _failed_in_body),
    (_SUREFIRE.search, _surefire),
    (_PHPUNIT_OK.search, _total_only),
    (_PHPUNIT.search, _phpunit),
    (_CARGO.search, _passed_failed),
    (_DOTNET_SUMMARY.search, _total_failures),
    (_dotnet_line, _dotnet),
    (_MOCHA_PASSING.search, _total_only),
    (_MOCHA_FAILING.search, _failed_tests),
    (_RSPEC.search, _total_failures),
    (_NODE_TESTS.search, _total_only),
    (_NODE_FAIL.search, _failures_only),
    (_CTEST.search, _failed_of_total),
    (_PESTER.search, _passed_failed),
    (_GRADLE.search, _total_failures),
)
