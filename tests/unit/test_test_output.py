"""Tests for utils/test_output.py — pytest outcome parsing, excerpts, signatures.

The IT* fixtures are verbatim pytest stdout from run 6f29441bbe8c
(test_report_002/003/005/006/007), the local-model run that stalled because
no consumer could read per-test outcomes.
"""

from __future__ import annotations

import re

from delegatron.utils.test_output import (
    build_results,
    extract_test_functions,
    failed_test_ids,
    failure_signature,
    format_test_failures,
    parse_pytest_outcomes,
    remove_failure_sections,
)

_HEADER = """\
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/python
cachedir: .pytest_cache
rootdir: /workspace
"""

# test_report_002: one NameError failure, summary message truncated by pytest.
IT2 = (
    _HEADER
    + """\
collecting ... collected 2 items

tests/test_card_deck.py::TestDeck::test_deal FAILED                      [ 50%]
tests/test_card_deck.py::TestDeck::test_shuffle PASSED                   [100%]

=================================== FAILURES ===================================
______________________________ TestDeck.test_deal ______________________________

self = <test_card_deck.TestDeck testMethod=test_deal>

    def test_deal(self):
        dealt_cards = self.deck.deal(2)
        self.assertEqual(len(dealt_cards), 2)
        for card in dealt_cards:
>           self.assertIn(card, original_cards)
                                ^^^^^^^^^^^^^^
E           NameError: name 'original_cards' is not defined

tests/test_card_deck.py:21: NameError
=========================== short test summary info ============================
FAILED tests/test_card_deck.py::TestDeck::test_deal - NameError: name 'origin...
========================= 1 failed, 1 passed in 0.11s ==========================
"""
)

# test_report_005: flaky shuffle test + deterministic print-count failure.
IT5 = (
    _HEADER
    + """\
collecting ... collected 7 items

tests/test_card_deck.py::TestDeck::test_deal PASSED                      [ 14%]
tests/test_card_deck.py::TestDeck::test_shuffle FAILED                   [ 28%]
tests/test_simulate_deal.py::TestSimulateDeal::test_card_creation PASSED [ 42%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deal_card PASSED     [ 57%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deck_initialization PASSED [ 71%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deck_shuffle PASSED  [ 85%]
tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal FAILED [100%]

=================================== FAILURES ===================================
____________________________ TestDeck.test_shuffle _____________________________

self = <test_card_deck.TestDeck testMethod=test_shuffle>

    def test_shuffle(self):
        original_cards = list(self.deck.cards)
        self.deck.shuffle()
>       self.assertNotEqual(original_cards, self.deck.cards)
E       AssertionError: [Card(name=Pikachu, type=Electric), Card(name=Eevee, type=Normal), Card(name=Charmander, type=Fire)] == [Card(name=Pikachu, type=Electric), Card(name=Eevee, type=Normal), Card(name=Charmander, type=Fire)]

tests/test_card_deck.py:15: AssertionError
_____________________ TestSimulateDeal.test_simulate_deal ______________________

self = <test_simulate_deal.TestSimulateDeal testMethod=test_simulate_deal>

    def test_simulate_deal(self):
        with mock.patch('simulate_deal.print') as mock_print:
            simulate_deal()
            calls = mock_print.call_args_list
>           self.assertEqual(len(calls), 10)  # 5 cards for Player 1, 5 cards for Player 2, and 2 newlines
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           AssertionError: 12 != 10

tests/test_simulate_deal.py:33: AssertionError
=========================== short test summary info ============================
FAILED tests/test_card_deck.py::TestDeck::test_shuffle - AssertionError: [Car...
FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - As...
========================= 2 failed, 5 passed in 0.23s ==========================
"""  # noqa: E501
)

# test_report_006: only the deterministic failure remains.
IT6 = (
    _HEADER
    + """\
collecting ... collected 7 items

tests/test_card_deck.py::TestDeck::test_deal PASSED                      [ 14%]
tests/test_card_deck.py::TestDeck::test_shuffle PASSED                   [ 28%]
tests/test_simulate_deal.py::TestSimulateDeal::test_card_creation PASSED [ 42%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deal_card PASSED     [ 57%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deck_initialization PASSED [ 71%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deck_shuffle PASSED  [ 85%]
tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal FAILED [100%]

=================================== FAILURES ===================================
_____________________ TestSimulateDeal.test_simulate_deal ______________________

self = <test_simulate_deal.TestSimulateDeal testMethod=test_simulate_deal>

    def test_simulate_deal(self):
        with mock.patch('simulate_deal.print') as mock_print:
            simulate_deal()
            calls = mock_print.call_args_list
>           self.assertEqual(len(calls), 10)  # 5 cards for Player 1, 5 cards for Player 2, and 2 newlines
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           AssertionError: 12 != 10

tests/test_simulate_deal.py:33: AssertionError
=========================== short test summary info ============================
FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - As...
========================= 1 failed, 6 passed in 0.21s ==========================
"""  # noqa: E501
)

# test_report_007: same test, both the code and the expectation moved.
IT7 = (
    _HEADER
    + """\
collecting ... collected 7 items

tests/test_card_deck.py::TestDeck::test_deal PASSED                      [ 14%]
tests/test_card_deck.py::TestDeck::test_shuffle PASSED                   [ 28%]
tests/test_simulate_deal.py::TestSimulateDeal::test_card_creation PASSED [ 42%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deal_card PASSED     [ 57%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deck_initialization PASSED [ 71%]
tests/test_simulate_deal.py::TestSimulateDeal::test_deck_shuffle PASSED  [ 85%]
tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal FAILED [100%]

=================================== FAILURES ===================================
_____________________ TestSimulateDeal.test_simulate_deal ______________________

self = <test_simulate_deal.TestSimulateDeal testMethod=test_simulate_deal>

    def test_simulate_deal(self):
        with mock.patch('simulate_deal.print') as mock_print:
            simulate_deal()
            calls = mock_print.call_args_list
>           self.assertEqual(len(calls), 12)  # 5 cards for Player 1, 5 cards for Player 2, and 2 newlines
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           AssertionError: 14 != 12

tests/test_simulate_deal.py:33: AssertionError
=========================== short test summary info ============================
FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - As...
========================= 1 failed, 6 passed in 0.22s ==========================
"""  # noqa: E501
)

# test_report_003: the passing run before t3 (two tests, both PASSED).
IT3 = (
    _HEADER
    + """\
collecting ... collected 2 items

tests/test_card_deck.py::TestDeck::test_deal PASSED                      [ 50%]
tests/test_card_deck.py::TestDeck::test_shuffle PASSED                   [100%]

============================== 2 passed in 0.10s ===============================
"""
)

# Setup/teardown errors, skip/xfail/xpass, parametrized ids (one with a space).
MIXED = (
    _HEADER
    + """\
collecting ... collected 10 items

tests/test_mix.py::test_ok PASSED                                        [ 10%]
tests/test_mix.py::test_skipped SKIPPED (not today)                      [ 20%]
tests/test_mix.py::test_xfail XFAIL (known bug)                          [ 30%]
tests/test_mix.py::test_xpass XPASS (fixed now)                          [ 40%]
tests/test_mix.py::test_setup_error ERROR                                [ 50%]
tests/test_mix.py::test_teardown_error PASSED                            [ 60%]
tests/test_mix.py::test_teardown_error ERROR                             [ 60%]
tests/test_mix.py::test_param[1-1] PASSED                                [ 70%]
tests/test_mix.py::test_param[2-3] FAILED                                [ 80%]
tests/test_mix.py::test_param[x y-x y] PASSED                            [ 90%]
tests/test_mix.py::TestThing::test_obj FAILED                            [100%]

==================================== ERRORS ====================================
______________________ ERROR at setup of test_setup_error ______________________

    @pytest.fixture
    def broken():
>       raise RuntimeError("fixture exploded")
E       RuntimeError: fixture exploded

tests/test_mix.py:5: RuntimeError
___________________ ERROR at teardown of test_teardown_error ___________________

    @pytest.fixture
    def bad_teardown():
        yield 1
>       raise RuntimeError("teardown exploded")
E       RuntimeError: teardown exploded

tests/test_mix.py:10: RuntimeError
=================================== FAILURES ===================================
_______________________________ test_param[2-3] ________________________________

a = 2, b = 3

    @pytest.mark.parametrize("a,b", [(1, 1), (2, 3), ("x y", "x y")])
    def test_param(a, b):
>       assert a == b
E       assert 2 == 3

tests/test_mix.py:35: AssertionError
______________________________ TestThing.test_obj ______________________________

self = <test_mix.TestThing object at 0x7f1c2b3a4d50>

    def test_obj(self):
        class Obj: pass
>       assert Obj() == Obj()
E       AssertionError: assert <Obj object at 0x7f1c2b3a4e10> == <Obj object at 0x7f1c2b3a4f90>

tests/test_mix.py:40: AssertionError
=========================== short test summary info ============================
FAILED tests/test_mix.py::test_param[2-3] - assert 2 == 3
FAILED tests/test_mix.py::TestThing::test_obj - AssertionError: assert <Obj obj...
ERROR tests/test_mix.py::test_setup_error - RuntimeError: fixture exploded
ERROR tests/test_mix.py::test_teardown_error - RuntimeError: teardown exploded
=== 2 failed, 4 passed, 1 skipped, 1 xfailed, 1 xpassed, 2 errors in 0.09s ===
"""
)

# Collection errors: a missing module and a SyntaxError in a test file.
COLLECTION_ERRORS = (
    _HEADER
    + """\
collecting ... collected 0 items / 2 errors

==================================== ERRORS ====================================
_______________ ERROR collecting tests/test_broken_import.py ________________
ImportError while importing test module '/workspace/tests/test_broken_import.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/local/lib/python3.12/importlib/__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/test_broken_import.py:1: in <module>
    from card_deck import Deck
E   ModuleNotFoundError: No module named 'card_deck'
____________________ ERROR collecting tests/test_syntax.py _____________________
/usr/local/lib/python3.12/site-packages/_pytest/python.py:507: in importtestmodule
    mod = import_path(
E     File "/workspace/tests/test_syntax.py", line 1
E       def test_y(:
E                  ^
E   SyntaxError: invalid syntax
=========================== short test summary info ============================
ERROR tests/test_broken_import.py
ERROR tests/test_syntax.py - SyntaxError: invalid syntax
!!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
============================== 2 errors in 0.17s ===============================
"""
)

GO_OUTPUT = (
    "=== RUN   TestA\n"
    "--- PASS: TestA (0.00s)\n"
    "=== RUN   TestB\n"
    "    fib_test.go:12: expected 55, got 0\n"
    "--- FAIL: TestB (0.00s)\n"
    "FAIL\n"
    "exit status 1\n"
    "FAIL\tfibtest\t0.001s\n"
)

# Jest writes to stderr, coloured.
JEST_TITLE = "sum \u203a adds 1 + 2 to equal 3"
JEST_STDERR = """\
\x1b[1m\x1b[31m FAIL \x1b[39m\x1b[22m src/sum.test.js
  \x1b[1m\u25cf sum \u203a adds 1 + 2 to equal 3\x1b[22m

    expect(received).toBe(expected) // Object.is equality

    Expected: \x1b[32m4\x1b[39m
    Received: \x1b[31m3\x1b[39m

      at Object.toBe (src/sum.test.js:4:21)

Test Suites: 1 failed, 1 total
Tests:       1 failed, 1 passed, 2 total
Snapshots:   0 total
Time:        0.512 s
Ran all test suites.
"""

CARGO_OUTPUT = """\
running 2 tests
test tests::it_works ... ok
test tests::it_fails ... FAILED

failures:

---- tests::it_fails stdout ----
thread 'tests::it_fails' panicked at src/lib.rs:10:9:
assertion `left == right` failed

failures:
    tests::it_fails

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; finished in 0.00s
"""


def _failed(stdout: str, stderr: str = "", **extra: object) -> dict[str, object]:
    return {"passed": False, "total": 1, "failures": 1, "stdout": stdout, "stderr": stderr, **extra}


class TestParsePytestOutcomes:
    def test_real_run_outcomes_in_order(self) -> None:
        assert parse_pytest_outcomes(IT5) == {
            "tests/test_card_deck.py::TestDeck::test_deal": "passed",
            "tests/test_card_deck.py::TestDeck::test_shuffle": "failed",
            "tests/test_simulate_deal.py::TestSimulateDeal::test_card_creation": "passed",
            "tests/test_simulate_deal.py::TestSimulateDeal::test_deal_card": "passed",
            "tests/test_simulate_deal.py::TestSimulateDeal::test_deck_initialization": "passed",
            "tests/test_simulate_deal.py::TestSimulateDeal::test_deck_shuffle": "passed",
            "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal": "failed",
        }
        assert (
            list(parse_pytest_outcomes(IT5))[1] == "tests/test_card_deck.py::TestDeck::test_shuffle"
        )

    def test_traceback_lines_are_not_outcomes(self) -> None:
        # "tests/test_card_deck.py:21: NameError" must not become a node id.
        assert parse_pytest_outcomes(IT2) == {
            "tests/test_card_deck.py::TestDeck::test_deal": "failed",
            "tests/test_card_deck.py::TestDeck::test_shuffle": "passed",
        }

    def test_all_statuses(self) -> None:
        outcomes = parse_pytest_outcomes(MIXED)
        assert outcomes == {
            "tests/test_mix.py::test_ok": "passed",
            "tests/test_mix.py::test_skipped": "skipped",
            "tests/test_mix.py::test_xfail": "xfailed",
            "tests/test_mix.py::test_xpass": "xpassed",
            "tests/test_mix.py::test_setup_error": "error",
            "tests/test_mix.py::test_teardown_error": "error",
            "tests/test_mix.py::test_param[1-1]": "passed",
            "tests/test_mix.py::test_param[2-3]": "failed",
            "tests/test_mix.py::test_param[x y-x y]": "passed",
            "tests/test_mix.py::TestThing::test_obj": "failed",
        }

    def test_teardown_error_overrides_pass_but_keeps_position(self) -> None:
        ids = list(parse_pytest_outcomes(MIXED))
        assert ids.index("tests/test_mix.py::test_teardown_error") == 5

    def test_summary_line_overrides_verbose_line(self) -> None:
        out = (
            "tests/test_a.py::test_x PASSED  [100%]\n"
            "=========================== short test summary info ============================\n"
            "ERROR tests/test_a.py::test_x - RuntimeError: teardown\n"
        )
        assert parse_pytest_outcomes(out) == {"tests/test_a.py::test_x": "error"}

    def test_quiet_mode_reads_summary_only(self) -> None:
        out = (
            "tests/test_a.py .F                                                       [100%]\n"
            "FAILED tests/test_a.py::test_two - assert 1 == 2\n"
            "1 failed, 1 passed in 0.01s\n"
        )
        assert parse_pytest_outcomes(out) == {"tests/test_a.py::test_two": "failed"}

    def test_report_all_summary(self) -> None:
        # -rA lists every outcome; SKIPPED lines carry file:line, not a node id.
        out = (
            "PASSED tests/test_mix.py::test_ok\n"
            "SKIPPED [1] tests/test_mix.py:15: not today\n"
            "XFAIL tests/test_mix.py::test_xfail - known bug\n"
            "XPASS tests/test_mix.py::test_xpass - fixed now\n"
        )
        assert parse_pytest_outcomes(out) == {
            "tests/test_mix.py::test_ok": "passed",
            "tests/test_mix.py::test_xfail": "xfailed",
            "tests/test_mix.py::test_xpass": "xpassed",
        }

    def test_collection_errors_use_module_path(self) -> None:
        assert parse_pytest_outcomes(COLLECTION_ERRORS) == {
            "tests/test_broken_import.py": "error",
            "tests/test_syntax.py": "error",
        }

    def test_ansi_colour_codes_stripped(self) -> None:
        out = (
            "tests/test_mix.py::test_ok \x1b[32mPASSED\x1b[0m\x1b[32m   [ 50%]\x1b[0m\n"
            "tests/test_mix.py::test_bad \x1b[31mFAILED\x1b[0m\x1b[31m  [100%]\x1b[0m\n"
            "\x1b[31mFAILED\x1b[0m tests/test_mix.py::\x1b[1mtest_bad\x1b[0m - assert 0\n"
        )
        assert parse_pytest_outcomes(out) == {
            "tests/test_mix.py::test_ok": "passed",
            "tests/test_mix.py::test_bad": "failed",
        }

    def test_xdist_progress_lines(self) -> None:
        out = (
            "tests/test_mix.py::test_ok \n"
            "[gw0] [ 50%] PASSED tests/test_mix.py::test_ok \n"
            "[gw1] [100%] FAILED tests/test_mix.py::test_bad \n"
        )
        assert parse_pytest_outcomes(out) == {
            "tests/test_mix.py::test_ok": "passed",
            "tests/test_mix.py::test_bad": "failed",
        }

    def test_windows_line_endings(self) -> None:
        assert parse_pytest_outcomes(IT6.replace("\n", "\r\n")) == parse_pytest_outcomes(IT6)

    def test_non_pytest_output_yields_nothing(self) -> None:
        for out in (GO_OUTPUT, JEST_STDERR, CARGO_OUTPUT, ""):
            assert parse_pytest_outcomes(out) == {}

    def test_pathological_line_is_fast(self) -> None:
        # Many "::" segments without a status must not backtrack exponentially.
        assert parse_pytest_outcomes("x.py" + "::a" * 5000 + " !") == {}


class TestBuildResults:
    def test_real_run_results(self) -> None:
        results = build_results(IT5)
        assert len(results) == 7
        by_name = {r["name"]: r for r in results}
        deal = by_name["tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"]
        assert deal == {
            "name": "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal",
            "passed": False,
            "status": "failed",
            "error": "AssertionError: 12 != 10",
        }
        assert by_name["tests/test_card_deck.py::TestDeck::test_deal"]["status"] == "passed"
        assert by_name["tests/test_card_deck.py::TestDeck::test_deal"]["error"] == ""

    def test_statuses_and_errors(self) -> None:
        by_name = {r["name"]: r for r in build_results(MIXED)}
        assert by_name["tests/test_mix.py::test_skipped"]["passed"] is True
        assert by_name["tests/test_mix.py::test_xfail"]["status"] == "xfailed"
        teardown = by_name["tests/test_mix.py::test_teardown_error"]
        assert (teardown["passed"], teardown["status"]) == (False, "error")
        assert teardown["error"] == "RuntimeError: teardown exploded"

    def test_cap_keeps_every_failure(self) -> None:
        results = build_results(IT5, cap=2)
        assert [r["passed"] for r in results] == [False, False]

    def test_signature_from_results_matches_the_output(self) -> None:
        report = {"passed": False, "results": build_results(IT6)}
        assert failure_signature(report) == failure_signature(_failed(IT6))


class TestFailedTestIds:
    def test_real_run_failures(self) -> None:
        assert failed_test_ids(IT5) == [
            "tests/test_card_deck.py::TestDeck::test_shuffle",
            "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal",
        ]
        assert failed_test_ids(IT6) == [
            "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
        ]

    def test_errors_and_failures_deduplicated_in_first_seen_order(self) -> None:
        assert failed_test_ids(MIXED) == [
            "tests/test_mix.py::test_setup_error",
            "tests/test_mix.py::test_teardown_error",
            "tests/test_mix.py::test_param[2-3]",
            "tests/test_mix.py::TestThing::test_obj",
        ]

    def test_collection_errors(self) -> None:
        assert failed_test_ids(COLLECTION_ERRORS) == [
            "tests/test_broken_import.py",
            "tests/test_syntax.py",
        ]

    def test_passing_run(self) -> None:
        out = (
            "tests/test_card_deck.py::TestDeck::test_deal PASSED     [ 50%]\n"
            "tests/test_card_deck.py::TestDeck::test_shuffle PASSED  [100%]\n"
            "============================== 2 passed in 0.12s ===============================\n"
        )
        assert failed_test_ids(out) == []

    def test_real_passing_run(self) -> None:
        assert failed_test_ids(IT3) == []

    def test_verbatim_it7(self) -> None:
        assert failed_test_ids(IT7) == [
            "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
        ]

    def test_go_failures(self) -> None:
        assert failed_test_ids(GO_OUTPUT) == ["TestB"]
        assert failed_test_ids("--- FAIL: TestBroken (0.00s)\nFAIL\n") == ["TestBroken"]

    def test_go_subtests_and_duplicates(self) -> None:
        out = (
            "=== RUN   TestFib\n"
            "=== RUN   TestFib/zero\n"
            "    --- FAIL: TestFib/zero (0.00s)\n"
            "--- FAIL: TestFib (0.00s)\n"
            "--- FAIL: TestFib (0.00s)\n"
        )
        assert failed_test_ids(out.replace("\n", "\r\n")) == ["TestFib/zero", "TestFib"]

    def test_other_runners_are_unknown(self) -> None:
        assert failed_test_ids(JEST_STDERR) == []
        assert failed_test_ids(CARGO_OUTPUT) == []

    def test_pytest_output_never_reads_go_lines(self) -> None:
        assert failed_test_ids(IT3 + "--- FAIL: TestX (0.00s)\n") == []


class TestFormatTestFailures:
    def test_real_run_excerpt(self) -> None:
        excerpt = format_test_failures(IT6)
        assert excerpt.startswith("=" * 35 + " FAILURES ")
        assert "\nE           AssertionError: 12 != 10\n" in excerpt
        assert "FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal" in excerpt
        assert excerpt.endswith("= 1 failed, 6 passed in 0.21s " + "=" * 26)
        assert "PASSED" not in excerpt
        assert "test session starts" not in excerpt

    def test_errors_banner_starts_excerpt(self) -> None:
        excerpt = format_test_failures(MIXED)
        assert excerpt.startswith("=" * 36 + " ERRORS ")
        assert "RuntimeError: fixture exploded" in excerpt
        assert "E       assert 2 == 3" in excerpt

    def test_collection_errors_include_interrupted_line(self) -> None:
        excerpt = format_test_failures(COLLECTION_ERRORS)
        assert "E   ModuleNotFoundError: No module named 'card_deck'" in excerpt
        assert "E   SyntaxError: invalid syntax" in excerpt
        assert "Interrupted: 2 errors during collection" in excerpt
        assert excerpt.endswith("= 2 errors in 0.17s " + "=" * 31)

    def test_output_after_final_line_is_dropped(self) -> None:
        excerpt = format_test_failures(IT6 + "\ndocker: container exited\n")
        assert "docker" not in excerpt

    def test_banner_without_summary_runs_to_end(self) -> None:
        out = IT2.split("=========================== short test summary info")[0]
        excerpt = format_test_failures(out)
        assert excerpt.startswith("=" * 35 + " FAILURES ")
        assert excerpt.endswith("tests/test_card_deck.py:21: NameError")

    def test_long_excerpt_keeps_head_and_summary(self) -> None:
        noise = "".join(f"captured line {i}\n" for i in range(3000))
        out = IT5.replace(
            "tests/test_card_deck.py:15: AssertionError\n",
            "tests/test_card_deck.py:15: AssertionError\n" + noise,
        )
        excerpt = format_test_failures(out, max_chars=4000)
        assert len(excerpt) <= 4000
        assert "\n...\n" in excerpt
        assert excerpt.startswith("=" * 35 + " FAILURES ")
        assert "E       AssertionError: [Card(name=Pikachu" in excerpt
        summary = out[out.index("=========================== short test summary info") :]
        assert excerpt.endswith(summary.rstrip())

    def test_huge_summary_is_capped(self) -> None:
        failed = "".join(
            f"FAILED tests/test_big.py::test_{i} - assert {i} == 0\n" for i in range(500)
        )
        out = (
            "=================================== FAILURES ===================================\n"
            "___ test_0 ___\nE   assert 0 == 1\n"
            "=========================== short test summary info ============================\n"
            + failed
            + "============================ 500 failed in 1.00s =============================\n"
        )
        excerpt = format_test_failures(out, max_chars=2000)
        assert len(excerpt) <= 2000
        assert excerpt.startswith("=" * 35 + " FAILURES ")
        assert "E   assert 0 == 1" in excerpt
        assert excerpt.endswith("500 failed in 1.00s " + "=" * 29)

    def test_long_tracebacks_keep_every_assertion(self) -> None:
        # The #69 regression: 80 traceback source lines before test_shuffle's
        # "E" line pushed both assertions out of a head+tail excerpt.
        source = "".join(
            f"    line_{i:02d} = deck.cards[{i} % len(deck.cards)]  # traceback source line\n"
            for i in range(80)
        )
        out = IT5.replace(
            ">       self.assertNotEqual(original_cards, self.deck.cards)\n",
            source + ">       self.assertNotEqual(original_cards, self.deck.cards)\n",
        )
        assert len(out) > 8000
        excerpt = format_test_failures(out)
        assert len(excerpt) <= 4000
        assert "E           AssertionError: 12 != 10" in excerpt
        assert "E       AssertionError: [Card(name=Pikachu" in excerpt
        assert ">           self.assertEqual(len(calls), 10)" in excerpt
        assert "tests/test_simulate_deal.py:33: AssertionError" in excerpt
        assert "_ TestDeck.test_shuffle _" in excerpt
        assert "line_40" not in excerpt
        assert excerpt.endswith("= 2 failed, 5 passed in 0.23s " + "=" * 26)

    def test_50kb_output_keeps_every_summary_line_and_assertions(self) -> None:
        blocks = "".join(
            f"{'_' * 20} test_case_{i} {'_' * 20}\n\n"
            + "".join(f"    noise_{i}_{j} = compute({j})\n" for j in range(30))
            + f">       assert result_{i} == expected_{i}\n"
            + f"E       AssertionError: assert {i} == {i + 1}\n"
            + f"E        +  where {i} = result_{i}\n\n"
            + f"tests/test_big.py:{10 + i}: AssertionError\n"
            for i in range(30)
        )
        summary = "".join(
            f"FAILED tests/test_big.py::test_case_{i} - AssertionError: assert {i} == {i + 1}\n"
            for i in range(30)
        )
        out = (
            "=================================== FAILURES ===================================\n"
            + blocks
            + "=========================== short test summary info ============================\n"
            + summary
            + "============================ 30 failed in 1.00s ==============================\n"
        )
        out += "x" * (50_000 - len(out)) if len(out) < 50_000 else ""
        excerpt = format_test_failures(out, max_chars=4000)
        assert len(excerpt) <= 4000
        for line in summary.splitlines():
            assert line in excerpt
        assert "E       AssertionError: assert 0 == 1" in excerpt
        assert "tests/test_big.py:10: AssertionError" in excerpt
        assert "noise_0_" not in excerpt

    def test_a_4kb_error_line_keeps_the_later_blocks(self) -> None:
        # #69: test_shuffle's E line for a 52-card deck (4 KB) made its block
        # bigger than the budget, and every later block was dropped with it.
        cards = ", ".join(f"Card(name=Pokemon{i}, type=Fire, hp={i * 10})" for i in range(52))
        long_e = f"E       AssertionError: [{cards}] == [{cards}]"
        assert len(long_e) > 4000
        out = re.sub(r"^E       AssertionError: \[Card.*$", long_e, IT5, count=1, flags=re.M)
        excerpt = format_test_failures(out, max_chars=4000)
        assert len(excerpt) <= 4000
        assert "\nE           AssertionError: 12 != 10\n" in excerpt
        assert "_ TestSimulateDeal.test_simulate_deal _" in excerpt
        assert "tests/test_simulate_deal.py:33: AssertionError" in excerpt
        assert "_ TestDeck.test_shuffle _" in excerpt
        shuffle_e = next(line for line in excerpt.splitlines() if "Pokemon0" in line)
        assert len(shuffle_e) == 500
        assert shuffle_e.endswith("...")
        assert long_e.startswith(shuffle_e[:-3])
        assert excerpt.endswith("= 2 failed, 5 passed in 0.23s " + "=" * 26)

    def test_a_block_too_big_for_the_room_never_drops_the_later_ones(self) -> None:
        # Many short E lines: the line cap alone cannot make the block fit.
        diff = "".join(f"E         - item {i:03d} {'x' * 80}\n" for i in range(80))
        out = IT5.replace(
            "tests/test_card_deck.py:15: AssertionError\n",
            diff + "\ntests/test_card_deck.py:15: AssertionError\n",
        )
        excerpt = format_test_failures(out, max_chars=4000)
        assert len(excerpt) <= 4000
        assert "E       AssertionError: [Card(name=Pikachu" in excerpt
        assert "E         - item 000" in excerpt
        assert "item 079" not in excerpt
        assert "\nE           AssertionError: 12 != 10\n" in excerpt
        assert "_ TestSimulateDeal.test_simulate_deal _" in excerpt
        for line in out[out.index("FAILED tests/") :].splitlines():
            assert line in excerpt

    def test_a_cut_block_leaves_room_for_the_next_without_a_summary(self) -> None:
        head = "=" * 35 + " FAILURES " + "=" * 35
        big = "___ test_big ___\n" + "".join(f"E   line {i} {'y' * 300}\n" for i in range(40))
        small = "___ test_small ___\nE   assert 1 == 2\n"
        out = f"{head}\n{big}{small}"
        excerpt = format_test_failures(out, max_chars=2000)
        assert len(excerpt) <= 2000
        assert "___ test_big ___" in excerpt
        assert "___ test_small ___\nE   assert 1 == 2" in excerpt

    def test_tiny_budget_never_exceeds_max_chars(self) -> None:
        for max_chars in (0, 3, 5, 6, 40, 120, 300):
            assert len(format_test_failures(IT5, max_chars=max_chars)) <= max_chars

    def test_jest_falls_back_to_tail_without_ansi(self) -> None:
        excerpt = format_test_failures(JEST_STDERR)
        assert f"\u25cf {JEST_TITLE}" in excerpt
        assert "\x1b" not in excerpt
        assert excerpt.endswith("Ran all test suites.")

    def test_fallback_truncates_to_last_chars(self) -> None:
        out = "x" * 10_000 + GO_OUTPUT
        excerpt = format_test_failures(out, max_chars=100)
        assert len(excerpt) == 100
        assert excerpt.endswith("FAIL\tfibtest\t0.001s")

    def test_empty_output(self) -> None:
        assert format_test_failures("") == ""
        assert format_test_failures("  \n\n") == ""


SHUFFLE = "tests/test_card_deck.py::TestDeck::test_shuffle"
SIMULATE = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"


class TestRemoveFailureSections:
    def test_removes_the_section_and_summary_line_of_an_id(self) -> None:
        out = remove_failure_sections(IT5, [SHUFFLE])
        assert "TestDeck.test_shuffle" not in out
        assert "Card(name=Pikachu" not in out
        assert f"FAILED {SHUFFLE}" not in out
        assert f"{SHUFFLE} FAILED" in out  # progress lines are not failure sections
        assert "_ TestSimulateDeal.test_simulate_deal _" in out
        assert "\nE           AssertionError: 12 != 10\n" in out
        assert f"FAILED {SIMULATE} - As..." in out
        assert "=" * 35 + " FAILURES " in out
        assert out.endswith("= 2 failed, 5 passed in 0.23s " + "=" * 26 + "\n")

    def test_works_on_a_failure_excerpt(self) -> None:
        excerpt = format_test_failures(IT5)
        out = remove_failure_sections(excerpt, [SIMULATE])
        assert "12 != 10" not in out
        assert SIMULATE not in out
        assert "E       AssertionError: [Card(name=Pikachu" in out
        assert f"FAILED {SHUFFLE}" in out
        assert out.startswith("=" * 35 + " FAILURES ")

    def test_removing_every_id_keeps_banners_and_result_line(self) -> None:
        out = remove_failure_sections(IT5, (SHUFFLE, SIMULATE))
        assert "AssertionError" not in out
        assert "FAILED tests/" not in out
        assert "short test summary info" in out
        assert "2 failed, 5 passed" in out

    def test_setup_and_teardown_error_sections(self) -> None:
        out = remove_failure_sections(MIXED, ["tests/test_mix.py::test_setup_error"])
        assert "ERROR at setup of test_setup_error" not in out
        assert "fixture exploded" not in out
        assert "ERROR at teardown of test_teardown_error" in out
        assert "ERROR tests/test_mix.py::test_teardown_error - RuntimeError" in out
        assert "E       assert 2 == 3" in out

    def test_param_ids_and_class_methods(self) -> None:
        ids = ["tests/test_mix.py::test_param[2-3]", "tests/test_mix.py::TestThing::test_obj"]
        out = remove_failure_sections(MIXED, ids)
        assert "assert 2 == 3" not in out
        assert "TestThing.test_obj" not in out
        assert "FAILED tests/" not in out
        assert "fixture exploded" in out

    def test_collection_error_sections_by_module_id(self) -> None:
        out = remove_failure_sections(COLLECTION_ERRORS, ["tests/test_broken_import.py"])
        assert "card_deck" not in out
        assert "ERROR tests/test_broken_import.py" not in out
        assert "E   SyntaxError: invalid syntax" in out
        assert "ERROR tests/test_syntax.py - SyntaxError" in out
        assert "Interrupted: 2 errors during collection" in out

    def test_a_title_shared_with_a_kept_id_keeps_its_section(self) -> None:
        other = "tests/test_other.py::TestDeck::test_shuffle"
        out = remove_failure_sections(IT5, [SHUFFLE], keep=[other])
        assert "_ TestDeck.test_shuffle _" in out
        assert f"FAILED {SHUFFLE}" not in out

    def test_ansi_and_crlf_output(self) -> None:
        coloured = IT5.replace(
            "FAILED tests/test_card_deck.py", "\x1b[31mFAILED\x1b[0m tests/test_card_deck.py"
        ).replace("\n", "\r\n")
        out = remove_failure_sections(coloured, [SHUFFLE])
        assert "TestDeck.test_shuffle" not in out
        assert "test_card_deck.py::TestDeck::test_shuffle - " not in out
        assert "\r\nE           AssertionError: 12 != 10\r\n" in out

    def test_no_ids_or_other_runners_unchanged(self) -> None:
        assert remove_failure_sections(IT5, []) == IT5
        assert remove_failure_sections(IT5.replace("\n", "\r\n"), ()) == IT5.replace("\n", "\r\n")
        assert remove_failure_sections(JEST_STDERR, [SHUFFLE]) == JEST_STDERR
        assert remove_failure_sections(GO_OUTPUT, ["TestB"]) == GO_OUTPUT
        assert remove_failure_sections(IT5, ["tests/test_x.py::test_unknown"]) == IT5


class TestFailureSignature:
    def test_no_report_or_passing_report(self) -> None:
        assert failure_signature(None) == ""
        assert failure_signature({"passed": True, "stdout": IT6}) == ""

    def test_real_run_signature(self) -> None:
        assert failure_signature(_failed(IT6)) == (
            "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
            " - AssertionError: <num> != <num>"
        )

    def test_sorted_and_paired_with_first_error_line(self) -> None:
        parts = failure_signature(_failed(IT5)).split("|")
        assert len(parts) == 2
        assert parts[0].startswith(
            "tests/test_card_deck.py::TestDeck::test_shuffle - AssertionError: [Card(name=Pikachu"
        )
        assert parts[1] == (
            "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
            " - AssertionError: <num> != <num>"
        )

    def test_same_failure_same_signature(self) -> None:
        rerun = IT6.replace("in 0.21s", "in 0.35s")
        assert failure_signature(_failed(IT6)) == failure_signature(_failed(rerun))

    def test_moved_values_keep_the_signature(self) -> None:
        # it6 -> it7 in the real run: both code and test moved (12 != 10 -> 14 != 12),
        # which is the same failure, not progress.
        assert failure_signature(_failed(IT6)) == failure_signature(_failed(IT7))

    def test_different_error_changes_signature(self) -> None:
        it4 = IT6.replace(
            "AssertionError: 12 != 10", "AttributeError: module 'unittest' has no attribute 'mock'"
        )
        assert failure_signature(_failed(it4)) != failure_signature(_failed(IT6))

    def test_literals_and_durations_masked(self) -> None:
        def sig(error: str) -> str:
            out = IT6.replace("AssertionError: 12 != 10", error)
            return failure_signature(_failed(out)).split(" - ", 1)[1]

        assert sig("AssertionError: 'Ace' != 'King'") == "AssertionError: <str> != <str>"
        assert sig('KeyError: "player_2"') == sig('KeyError: "player_7"')
        assert sig("TimeoutError: took 1.25s (limit 0.5 s)") == (
            "TimeoutError: took <dur> (limit <dur>)"
        )
        assert sig("AssertionError: -3.5e2 != 1e-3") == "AssertionError: <num> != <num>"
        assert sig("NameError: name 'x2' is not defined") == "NameError: name <str> is not defined"
        assert sig("ValueError: bad test_2 in utf8") == "ValueError: bad test_2 in utf8"

    def test_results_errors_are_preferred(self) -> None:
        name = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"

        def report(error: str) -> dict[str, object]:
            return {
                "passed": False,
                "results": [
                    {
                        "name": "tests/test_simulate_deal.py::TestSimulateDeal::test_ok",
                        "passed": True,
                        "error": "",
                    },
                    {"name": name, "passed": False, "error": error},
                ],
            }

        first = failure_signature(report("AssertionError: 12 != 10"))
        assert first == f"{name} - AssertionError: <num> != <num>"
        assert failure_signature(report("AssertionError: 14 != 12")) == first
        other = failure_signature(
            report("AttributeError: module 'unittest' has no attribute 'mock'")
        )
        assert other != first
        assert other == f"{name} - AttributeError: module <str> has no attribute <str>"

    def test_results_error_wins_over_the_output(self) -> None:
        name = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
        report = _failed(IT6, results=[{"name": name, "passed": False, "error": "KeyError: 1"}])
        assert failure_signature(report) == f"{name} - KeyError: <num>"

    def test_results_without_error_use_the_output(self) -> None:
        name = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
        report = _failed(IT6, results=[{"name": name, "passed": False, "error": ""}])
        assert failure_signature(report) == failure_signature(_failed(IT6))

    def test_memory_addresses_masked(self) -> None:
        rerun = MIXED.replace("0x7f1c2b3a4e10", "0x55d0e1f2a3b4")
        sig = failure_signature(_failed(MIXED))
        assert sig == failure_signature(_failed(rerun))
        assert "0x7f1c2b3a4e10" not in sig

    def test_errors_and_param_ids(self) -> None:
        assert failure_signature(_failed(MIXED)).split("|") == [
            "tests/test_mix.py::TestThing::test_obj - AssertionError: assert"
            " <Obj object at 0x?> == <Obj object at 0x?>",
            "tests/test_mix.py::test_param[2-3] - assert <num> == <num>",
            "tests/test_mix.py::test_setup_error - RuntimeError: fixture exploded",
            "tests/test_mix.py::test_teardown_error - RuntimeError: teardown exploded",
        ]

    def test_collection_errors(self) -> None:
        assert failure_signature(_failed(COLLECTION_ERRORS)) == (
            "tests/test_broken_import.py - ModuleNotFoundError: No module named <str>"
            "|tests/test_syntax.py - File <str>, line <num>"
        )

    def test_same_function_name_in_two_files(self) -> None:
        out = (
            "tests/test_a.py::test_x FAILED\n"
            "tests/test_b.py::test_x FAILED\n"
            "=================================== FAILURES ===================================\n"
            "____________________________________ test_x ____________________________________\n"
            "E       assert 'a' == 'A'\n"
            "____________________________________ test_x ____________________________________\n"
            "E       assert 'b' == 'B'\n"
        )
        assert failure_signature(_failed(out)) == (
            "tests/test_a.py::test_x - assert <str> == <str>"
            "|tests/test_b.py::test_x - assert <str> == <str>"
        )

    def test_prefers_report_failed_tests(self) -> None:
        # Only the tracebacks survive in stdout; the ids come from the report.
        start = IT6.index("=" * 35 + " FAILURES")
        tracebacks = IT6[start : IT6.index("=" * 27 + " short test summary")]
        assert failed_test_ids(tracebacks) == []
        report = _failed(
            tracebacks,
            failed_tests=["tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"],
        )
        assert failure_signature(report) == failure_signature(_failed(IT6))

    def test_id_without_traceback(self) -> None:
        out = "FAILED tests/test_a.py::test_x\n"
        assert failure_signature(_failed(out)) == "tests/test_a.py::test_x"

    def test_go_failure_names_ignore_timings(self) -> None:
        slower = GO_OUTPUT.replace("0.001s", "0.004s")
        assert failure_signature(_failed(GO_OUTPUT)) == "TestB"
        assert failure_signature(_failed(slower)) == "TestB"

    def test_jest_failure_names(self) -> None:
        assert failure_signature(_failed("", JEST_STDERR)) == JEST_TITLE
        # The "Summary of all failing tests" block repeats titles; CRLF must not leak in.
        repeated = JEST_STDERR + "Summary of all failing tests\n" + JEST_STDERR
        assert failure_signature(_failed("", repeated.replace("\n", "\r\n"))) == JEST_TITLE

    def test_cargo_failure_names(self) -> None:
        assert failure_signature(_failed(CARGO_OUTPUT)) == "tests::it_fails"

    def test_unknown_runner_uses_last_stderr_line(self) -> None:
        report = _failed("some output\n", "Traceback...\nImportError:   cannot   import x\n\n")
        assert failure_signature(report) == "ImportError: cannot import x"

    def test_unknown_runner_falls_back_to_stdout(self) -> None:
        assert failure_signature(_failed("building...\nerror: build failed\n")) == (
            "error: build failed"
        )

    def test_failed_report_without_output(self) -> None:
        assert failure_signature(_failed("", "")) == ""


class TestExtractTestFunctions:
    def test_functions_methods_and_async(self) -> None:
        source = """\
import pytest


def helper():
    pass


def test_top():
    def test_inner():
        pass


async def test_async_top():
    pass


class TestDeck:
    def setUp(self):
        pass

    def test_deal(self):
        pass

    async def test_async_method(self):
        pass

    class TestNested:
        def test_deep(self):
            pass


class CardTests(unittest.TestCase):
    def testCamelCase(self):
        pass
"""
        assert extract_test_functions(source) == {
            "test_top",
            "test_async_top",
            "TestDeck::test_deal",
            "TestDeck::test_async_method",
            "TestDeck::TestNested::test_deep",
            "CardTests::testCamelCase",
        }

    def test_syntax_error_returns_empty(self) -> None:
        assert extract_test_functions("def test_y(:\n    pass\n") == set()

    def test_null_bytes_return_empty(self) -> None:
        assert extract_test_functions("def test_x():\x00 pass\n") == set()

    def test_no_tests(self) -> None:
        assert extract_test_functions("") == set()
        assert extract_test_functions("x = 1\n") == set()
