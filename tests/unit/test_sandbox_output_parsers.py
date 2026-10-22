"""Tests for DockerSandbox._parse_test_output with real multi-language output.

Regression samples captured from 2026-04-17 E2E runs where the parser
under-counted Go / Rust / Java tests and returned total=0 even when tests
passed (cosmetic display bug — the sandbox still reported `passed=True`
based on container exit code).
"""

from __future__ import annotations

from pathlib import Path

from delegatron.sandbox.docker_runner import DockerSandbox


def _parse(text: str) -> dict[str, int]:
    sb = DockerSandbox(workspace_root=Path("."))
    return sb._parse_test_output(text)


class TestGoParser:
    def test_verbose_per_test_pass_lines(self) -> None:
        """`go test -v` prints `--- PASS: TestName`."""
        out = """\
=== RUN   TestFib
--- PASS: TestFib (0.00s)
=== RUN   TestOther
--- PASS: TestOther (0.00s)
PASS
ok  	fibtest	0.001s
"""
        r = _parse(out)
        assert r["total"] == 2
        assert r["failures"] == 0

    def test_verbose_with_failures(self) -> None:
        out = """\
=== RUN   TestA
--- PASS: TestA (0.00s)
=== RUN   TestB
--- FAIL: TestB (0.00s)
    fib_test.go:12: expected 55, got 0
FAIL
FAIL	fibtest	0.001s
"""
        r = _parse(out)
        assert r["total"] == 2
        assert r["failures"] == 1

    def test_non_verbose_ok_summary(self) -> None:
        """Non-verbose `go test` just prints `ok PKG TIME`.  We can at
        least signal "at least one test ran and passed" by setting
        total=1; finer counts require `-v`."""
        r = _parse("ok  	fibtest	0.001s\n")
        assert r["total"] == 1
        assert r["failures"] == 0

    def test_non_verbose_fail_summary(self) -> None:
        r = _parse("FAIL	fibtest	0.5s\n")
        assert r["total"] == 1
        assert r["failures"] == 1


class TestRustParser:
    def test_unit_and_integration_results_sum(self) -> None:
        """Cargo emits two `test result:` lines (unit + integration
        tests) — we must sum across them, not let the empty integration
        block reset the count to 0."""
        out = """\
running 4 tests
test tests::test_fib_0 ... ok
test tests::test_fib_1 ... ok
test tests::test_fib_10 ... ok
test tests::test_fib_5 ... ok

test result: ok. 4 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s


running 0 tests

test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""
        r = _parse(out)
        assert r["total"] == 4, "must count unit tests despite trailing empty integration block"
        assert r["failures"] == 0

    def test_with_failures(self) -> None:
        out = "test result: FAILED. 2 passed; 3 failed; 0 ignored; 0 measured; 0 filtered out\n"
        r = _parse(out)
        assert r["total"] == 5
        assert r["failures"] == 3


class TestMavenSurefireParser:
    def test_summary_line_matches_not_per_file(self) -> None:
        """Surefire prints per-file lines (with `-- in ClassName`) AND a
        final summary line (no `-- in`).  We should parse the summary,
        not the per-file lines."""
        out = """\
[INFO]  T E S T S
[INFO] -------------------------------------------------------
[INFO] Running com.example.FibTest
[INFO] Tests run: 4, Failures: 0, Errors: 0, Time elapsed: 0.052 s -- in com.example.FibTest
[INFO]
[INFO] Results:
[INFO]
[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0
[INFO]
[INFO] BUILD SUCCESS
"""
        r = _parse(out)
        assert r["total"] == 4
        assert r["failures"] == 0

    def test_summary_with_failures_and_errors(self) -> None:
        out = """\
[INFO] Tests run: 7, Failures: 2, Errors: 1, Skipped: 0
"""
        r = _parse(out)
        assert r["total"] == 7
        assert r["failures"] == 3  # failures + errors both count as failures

    def test_multiple_test_classes_uses_summary(self) -> None:
        """Two per-file lines (`-- in FooTest` + `-- in BarTest`) but one
        summary — summary wins."""
        out = """\
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.01 s -- in FooTest
[INFO] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.02 s -- in BarTest
[INFO] Results:
[INFO] Tests run: 8, Failures: 1, Errors: 0, Skipped: 0
"""
        r = _parse(out)
        assert r["total"] == 8
        assert r["failures"] == 1


class TestPytestParserRegressionIntact:
    """Verify existing pytest behavior unchanged by the new patterns."""

    def test_pytest_all_passing(self) -> None:
        out = "============================== 3 passed in 0.06s ===============================\n"
        r = _parse(out)
        assert r["total"] == 3
        assert r["failures"] == 0

    def test_pytest_with_failures(self) -> None:
        out = "== 2 failed, 3 passed in 0.5s ==\n"
        r = _parse(out)
        assert r["total"] == 5
        assert r["failures"] == 2


class TestPHPUnitParser:
    def test_phpunit_ok_summary(self) -> None:
        out = """\
PHPUnit 10.5.63 by Sebastian Bergmann and contributors.

....                                                                4 / 4 (100%)

Time: 00:00.028, Memory: 8.00 MB

OK (4 tests, 4 assertions)
"""
        r = _parse(out)
        assert r["total"] == 4
        assert r["failures"] == 0

    def test_phpunit_single_test_ok(self) -> None:
        r = _parse("OK (1 test, 1 assertion)\n")
        assert r["total"] == 1
        assert r["failures"] == 0

    def test_phpunit_failures(self) -> None:
        out = "FAILURES!\nTests: 5, Assertions: 10, Failures: 2.\n"
        r = _parse(out)
        assert r["total"] == 5
        assert r["failures"] == 2

    def test_phpunit_failures_and_errors(self) -> None:
        out = "Tests: 7, Assertions: 15, Failures: 2, Errors: 1.\n"
        r = _parse(out)
        assert r["total"] == 7
        assert r["failures"] == 3


class TestVitestParser:
    def test_vitest_passing_summary(self) -> None:
        """Vitest prints two 'X passed' lines — one for Test Files,
        one for Tests.  Only the Tests line counts."""
        out = """\
 RUN  v2.1.9 /workspace

 ✓ Counter.test.tsx (2 tests) 20ms

 Test Files  1 passed (1)
      Tests  2 passed (2)
"""
        r = _parse(out)
        assert r["total"] == 2
        assert r["failures"] == 0

    def test_vitest_failing_summary(self) -> None:
        out = """\
 Test Files  1 failed (1)
      Tests  1 failed | 1 passed (2)
"""
        r = _parse(out)
        assert r["total"] == 2
        assert r["failures"] == 1


class TestJestParserRegressionIntact:
    def test_jest_summary_line(self) -> None:
        out = "Tests:       2 failed, 5 passed, 7 total\n"
        r = _parse(out)
        assert r["total"] == 7
        assert r["failures"] == 2


class TestPytestCountsComeFromTheSummaryOnly:
    """#69: the old greedy pattern counted every "N error" anywhere in the output."""

    def test_collection_error_counted_once(self) -> None:
        out = """\
collecting ... collected 0 items / 1 error

==================================== ERRORS ====================================
___________________ ERROR collecting tests/test_cards.py ______________________
E   ModuleNotFoundError: No module named 'cards'
=========================== short test summary info ============================
ERROR tests/test_cards.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.12s ===============================
"""
        assert _parse(out) == {"total": 1, "failures": 1}

    def test_assertion_text_does_not_inflate_counts(self) -> None:
        out = """\
tests/test_report.py::test_summary FAILED                                [100%]
E       AssertionError: assert '2 errors found' == '3 errors found'
E         - 3 errors found
E         + 2 errors found
FAILED tests/test_report.py::test_summary - AssertionError: assert '2 errors found' == '3 err...
============================== 1 failed in 0.03s ===============================
"""
        assert _parse(out) == {"total": 1, "failures": 1}

    def test_quiet_summary_with_warnings_and_skips(self) -> None:
        out = "3 failed, 10 passed, 2 skipped, 1 xfailed, 4 warnings in 1.20s\n"
        assert _parse(out) == {"total": 14, "failures": 3}

    def test_two_pytest_commands_add_up(self) -> None:
        assert _parse("== 2 passed in 0.1s ==\n== 1 failed, 1 passed in 0.2s ==\n") == {
            "total": 4,
            "failures": 1,
        }


class TestMoreRunners:
    """Passing runs these runners report used to give total=0 ("untested")."""

    def test_mocha(self) -> None:
        assert _parse("  3 passing (12ms)\n  1 pending\n  1 failing\n") == {
            "total": 4,
            "failures": 1,
        }

    def test_modern_dotnet(self) -> None:
        out = (
            "Passed!  - Failed:     0, Passed:    12, Skipped:     0, Total:    12, "
            "Duration: 25 ms - App.Tests.dll (net9.0)\n"
        )
        assert _parse(out) == {"total": 12, "failures": 0}

    def test_dotnet_9_summary(self) -> None:
        out = "Test summary: total: 12, failed: 1, succeeded: 11, skipped: 0, duration: 1.2s\n"
        assert _parse(out) == {"total": 12, "failures": 1}

    def test_vstest_block(self) -> None:
        out = "Total tests: 12\n     Passed: 11\n     Failed: 1\n Total time: 1.2 Seconds\n"
        assert _parse(out) == {"total": 12, "failures": 1}

    def test_rspec(self) -> None:
        assert _parse("5 examples, 1 failure\n") == {"total": 5, "failures": 1}

    def test_unittest_and_django(self) -> None:
        assert _parse("Ran 5 tests in 0.010s\n\nOK\n") == {"total": 5, "failures": 0}
        assert _parse("Ran 5 tests in 0.010s\n\nFAILED (failures=1, errors=1)\n") == {
            "total": 5,
            "failures": 2,
        }

    def test_ctest(self) -> None:
        assert _parse("75% tests passed, 1 tests failed out of 4\n") == {
            "total": 4,
            "failures": 1,
        }

    def test_pester(self) -> None:
        assert _parse("Tests Passed: 4, Failed: 1, Skipped: 0 NotRun: 0\n") == {
            "total": 5,
            "failures": 1,
        }

    def test_node_test_runner(self) -> None:
        assert _parse("# tests 5\n# pass 4\n# fail 1\n") == {"total": 5, "failures": 1}

    def test_gradle_failure_summary(self) -> None:
        assert _parse("4 tests completed, 1 failed\n") == {"total": 4, "failures": 1}

    def test_go_cached_package(self) -> None:
        assert _parse("ok  \tfibtest\t(cached)\n") == {"total": 1, "failures": 0}

    def test_go_verbose_ignores_the_package_line(self) -> None:
        out = "--- PASS: TestA (0.00s)\n--- PASS: TestB (0.00s)\nPASS\nok  \tpkg\t0.01s\n"
        assert _parse(out) == {"total": 2, "failures": 0}

    def test_ansi_colours_are_ignored(self) -> None:
        assert _parse("\x1b[32m3 passing\x1b[0m (12ms)\n") == {"total": 3, "failures": 0}

    def test_nothing_recognised(self) -> None:
        assert _parse("BUILD SUCCESSFUL in 2s\n") == {"total": 0, "failures": 0}
