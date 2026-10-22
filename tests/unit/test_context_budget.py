"""Tests for utils/context_budget.py — token estimate and input trimming."""

from __future__ import annotations

import json
from typing import Any

from delegatron.context import TRUNCATION_MARKER_PATTERN
from delegatron.utils.context_budget import (
    DEFAULT_TRIM_KEYS,
    MIN_EXCERPT_CHARS,
    estimate_tokens,
    excerpt,
    fit_input_to_budget,
)


def _render(data: dict[str, Any]) -> str:
    return json.dumps(data)


class TestEstimateTokens:
    def test_empty_text_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_rounds_up(self) -> None:
        assert estimate_tokens("x" * 7) == 2
        assert estimate_tokens("x" * 8) == 3


class TestFitInputToBudget:
    def test_input_within_budget_is_returned_unchanged(self) -> None:
        data = {"task": "t1", "repo_map": "a.py: class A"}
        fit = fit_input_to_budget(data, _render, budget_tokens=1000)
        assert fit.data == data
        assert fit.data is not data
        assert fit.trimmed == []
        assert fit.still_over is False
        assert fit.tokens_before == fit.tokens_after == estimate_tokens(_render(data))

    def test_does_not_mutate_the_input(self) -> None:
        files = {"a.py": "a" * 4000, "b.py": "b" * 100}
        data = {"task": "t1", "existing_files": files, "repo_map": "m" * 4000}
        fit_input_to_budget(data, _render, budget_tokens=50, protected=["a.py"])
        assert data["existing_files"] is files
        assert files == {"a.py": "a" * 4000, "b.py": "b" * 100}
        assert data["repo_map"] == "m" * 4000

    def test_dict_loses_its_last_entries_first(self) -> None:
        """Entries come most relevant first, so the least relevant go first (#75)."""
        files = {"big.py": "x" * 3000, "medium.py": "y" * 1500, "small.py": "z" * 100}
        data = {"task": "t1", "existing_files": files}
        fit = fit_input_to_budget(data, _render, budget_tokens=600)
        assert list(fit.data["existing_files"]) == []
        assert fit.dropped == {"existing_files": ["small.py", "medium.py", "big.py"]}
        assert fit.trimmed == ["existing_files"]
        assert estimate_tokens(_render(fit.data)) <= 600

    def test_dict_stops_dropping_once_it_fits(self) -> None:
        files = {"first.py": "x" * 1000, "second.py": "y" * 1500, "third.py": "z" * 1500}
        fit = fit_input_to_budget({"existing_files": files}, _render, budget_tokens=500)
        assert list(fit.data["existing_files"]) == ["first.py"]
        assert fit.dropped == {"existing_files": ["third.py", "second.py"]}

    def test_string_is_cut_from_the_end(self) -> None:
        repo_map = "".join(f"module_{i}.py: class C{i}\n" for i in range(400))
        data = {"task": "t1", "repo_map": repo_map}
        fit = fit_input_to_budget(data, _render, budget_tokens=500)
        assert fit.trimmed == ["repo_map"]
        assert fit.data["repo_map"].startswith("module_0.py: class C0\n")
        assert "truncated" in fit.data["repo_map"]
        assert estimate_tokens(_render(fit.data)) <= 500

    def test_keys_are_trimmed_in_order_and_only_as_needed(self) -> None:
        data = {
            "task": "t1",
            "repo_map": "m" * 3000,
            "related_files": {"r.py": "r" * 500},
            "existing_files": {"e.py": "e" * 500},
        }
        fit = fit_input_to_budget(data, _render, budget_tokens=400)
        assert fit.trimmed == ["repo_map"]
        assert fit.data["related_files"] == data["related_files"]
        assert fit.data["existing_files"] == data["existing_files"]

    def test_text_dropped_when_nothing_of_it_fits(self) -> None:
        data = {"task": "t" * 1000, "repo_map": "m" * 1000, "existing_files": {"e.py": "e"}}
        fit = fit_input_to_budget(data, _render, budget_tokens=300)
        assert "repo_map" not in fit.data
        assert fit.trimmed == ["repo_map"]

    def test_untrimmable_input_stays_over_budget(self) -> None:
        """Keys outside trim_keys are never touched, even when still over budget."""
        data = {"task": "t" * 2000, "repo_map": "m" * 100, "existing_files": {"e.py": "e" * 100}}
        fit = fit_input_to_budget(data, _render, budget_tokens=100)
        assert fit.data["task"] == data["task"]
        assert "repo_map" not in fit.data
        assert fit.data["existing_files"] == {}
        assert fit.trimmed == ["repo_map", "existing_files"]
        assert fit.still_over is True

    def test_missing_and_empty_keys_are_skipped(self) -> None:
        data = {"task": "t1", "repo_map": "", "existing_files": {"e.py": "e" * 3000}}
        fit = fit_input_to_budget(data, _render, budget_tokens=200)
        assert fit.trimmed == ["existing_files"]
        assert fit.data["repo_map"] == ""

    def test_other_value_types_are_dropped(self) -> None:
        data = {"task": "t1", "repo_map": ["m" * 3000]}
        fit = fit_input_to_budget(data, _render, budget_tokens=100)
        assert "repo_map" not in fit.data
        assert fit.trimmed == ["repo_map"]

    def test_custom_trim_keys(self) -> None:
        data = {"changed_files": {"a.py": "a" * 3000}, "repo_map": "m" * 3000}
        fit = fit_input_to_budget(data, _render, budget_tokens=1000, trim_keys=("changed_files",))
        assert fit.trimmed == ["changed_files"]
        assert fit.data["repo_map"] == data["repo_map"]

    def test_default_trim_order(self) -> None:
        assert DEFAULT_TRIM_KEYS == ("repo_map", "related_files", "existing_files")


class TestProtectedFiles:
    """Files the attempt must edit are excerpted, never dropped (#75, D10)."""

    def test_protected_files_survive_and_others_go(self) -> None:
        files = {"src/deal.py": "d" * 2000, "tests/test_deal.py": "t" * 2000, "util.py": "u" * 900}
        fit = fit_input_to_budget(
            {"existing_files": files},
            _render,
            budget_tokens=1300,
            protected=["src/deal.py", "./tests/test_deal.py"],
        )
        assert list(fit.data["existing_files"]) == ["src/deal.py", "tests/test_deal.py"]
        assert fit.dropped == {"existing_files": ["util.py"]}
        assert fit.excerpted == {}

    def test_protected_files_become_excerpts_when_nothing_else_is_left(self) -> None:
        body = "".join(f"line {i}\n" for i in range(2000))
        fit = fit_input_to_budget(
            {"existing_files": {"engine.py": body, "notes.py": "n" * 500}},
            _render,
            budget_tokens=800,
            protected=["engine.py"],
        )
        shown = fit.data["existing_files"]
        assert list(shown) == ["engine.py"]
        assert shown["engine.py"].startswith("line 0\n")
        assert shown["engine.py"].endswith("line 1999\n")
        assert TRUNCATION_MARKER_PATTERN.search(shown["engine.py"])
        assert fit.excerpted == {"existing_files": ["engine.py"]}
        assert fit.dropped == {"existing_files": ["notes.py"]}
        assert fit.still_over is False
        assert estimate_tokens(_render(fit.data)) <= 800

    def test_excerpt_floor_reports_still_over(self) -> None:
        fit = fit_input_to_budget(
            {"existing_files": {"a.py": "a" * 5000}},
            _render,
            budget_tokens=10,
            protected=["a.py"],
        )
        assert len(fit.data["existing_files"]["a.py"]) <= MIN_EXCERPT_CHARS + 1
        assert fit.still_over is True

    def test_local_window_scenario_keeps_the_files_under_repair(self) -> None:
        """A 4096-token window, 40 KB of files and a repo map (the #75 probe)."""
        files = {
            "src/deal.py": "def deal():\n" + "    pass\n" * 1400,  # task output, 12.6 KB
            "tests/test_deal.py": "def test_deal():\n" + "    assert 1\n" * 600,  # failing
            "src/util.py": "x = 1\n" * 400,  # 2.4 KB, unrelated
            "src/cards.py": "y = 2\n" * 2000,
            "src/players.py": "z = 3\n" * 1500,
        }
        data = {
            "task": {"outputs": ["src/deal.py"]},
            "repo_map": "m" * 8000,
            "existing_files": files,
        }
        fit = fit_input_to_budget(
            data, _render, budget_tokens=2000, protected=["src/deal.py", "tests/test_deal.py"]
        )
        assert fit.trimmed[0] == "repo_map"
        assert "repo_map" not in fit.data
        assert set(fit.data["existing_files"]) == {"src/deal.py", "tests/test_deal.py"}
        assert fit.dropped == {"existing_files": ["src/players.py", "src/cards.py", "src/util.py"]}
        assert set(fit.excerpted["existing_files"]) <= {"src/deal.py", "tests/test_deal.py"}
        assert fit.still_over is False
        report = fit.report(window=4096, budget=2000)
        assert report["keys"] == ["repo_map", "existing_files"]
        assert report["dropped"] == fit.dropped
        assert report["window"] == 4096
        assert report["tokens_before"] > report["tokens_after"]
        assert report["still_over"] is False

    def test_fitting_is_idempotent(self) -> None:
        files = {"a.py": "a" * 3000, "b.py": "b" * 3000, "c.py": "c" * 3000}
        data = {"repo_map": "m" * 3000, "existing_files": files}
        once = fit_input_to_budget(data, _render, budget_tokens=900, protected=["a.py"])
        twice = fit_input_to_budget(once.data, _render, budget_tokens=900, protected=["a.py"])
        assert twice.data == once.data
        assert twice.trimmed == []


class TestExcerpt:
    def test_short_content_is_unchanged(self) -> None:
        assert excerpt("abc", 10) == "abc"

    def test_excerpt_keeps_head_and_tail_and_counts_the_cut_bytes(self) -> None:
        content = "h" * 500 + "é" * 1000 + "t" * 500
        result = excerpt(content, 400)
        assert len(result) <= 400
        assert result.startswith("h")
        assert result.endswith("t")
        match = TRUNCATION_MARKER_PATTERN.search(result)
        assert match is not None
        head = result[: match.start() - 1]
        tail = result[match.end() + 1 :]
        cut = content[len(head) : len(content) - len(tail)]
        assert match.group(0) == f"... [truncated {len(cut.encode('utf-8'))} bytes] ..."
