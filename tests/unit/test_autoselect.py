"""Tests for automatic preset selection based on task complexity."""

from delegatron.autoselect import select_preset


class TestSelectPresetFastKeywords:
    """Tests for requests that contain fast/simple keywords."""

    def test_simple_task_returns_budget(self):
        preset, reason = select_preset("simple fix to the readme", {"languages": ["python"]})
        assert preset == "budget"
        assert "simple" in reason

    def test_typo_fix_returns_budget(self):
        preset, reason = select_preset("fix a typo in the docs", {"languages": ["python"]})
        assert preset == "budget"
        assert "typo" in reason

    def test_quick_task_returns_budget(self):
        preset, reason = select_preset("quick change to config", {"languages": ["javascript"]})
        assert preset == "budget"
        assert "quick" in reason

    def test_rename_task_returns_budget(self):
        preset, reason = select_preset("rename the variable foo", {"languages": []})
        assert preset == "budget"
        assert "rename" in reason

    def test_minor_task(self):
        preset, _reason = select_preset("minor update to logging", {"languages": ["typescript"]})
        assert preset == "budget"

    def test_bump_version(self):
        preset, _reason = select_preset("bump the version to 2.0", {"languages": ["python"]})
        assert preset == "budget"

    def test_trivial_task(self):
        preset, _reason = select_preset("trivial cleanup", {"languages": []})
        assert preset == "budget"

    def test_small_task(self):
        preset, _reason = select_preset("small UI tweak", {"languages": ["javascript"]})
        assert preset == "budget"

    def test_version_keyword(self):
        preset, _reason = select_preset("update the version string", {"languages": ["python"]})
        assert preset == "budget"


class TestSelectPresetHeavyKeywords:
    """Tests for requests that contain complex/heavy keywords."""

    def test_security_task_returns_heavy(self):
        preset, reason = select_preset("implement security audit", {})
        assert preset == "claude-heavy"
        assert "security" in reason

    def test_refactor_task_returns_heavy(self):
        preset, reason = select_preset("refactor the entire codebase", {})
        assert preset == "claude-heavy"
        assert "refactor" in reason

    def test_complex_task(self):
        preset, reason = select_preset("complex multi-step migration", {})
        assert preset == "claude-heavy"
        assert "complex" in reason

    def test_architecture_task(self):
        preset, _reason = select_preset("redesign the architecture", {})
        assert preset == "claude-heavy"

    def test_migrate_task(self):
        preset, reason = select_preset("migrate from postgres to mysql", {})
        assert preset == "claude-heavy"
        assert "migrate" in reason

    def test_performance_task(self):
        preset, _reason = select_preset("performance optimization of queries", {})
        assert preset == "claude-heavy"

    def test_optimize_task(self):
        preset, _reason = select_preset("optimize the build pipeline", {})
        assert preset == "claude-heavy"

    def test_concurrent_task(self):
        preset, _reason = select_preset("add concurrent processing", {})
        assert preset == "claude-heavy"

    def test_async_task(self):
        preset, _reason = select_preset("convert to async handlers", {})
        assert preset == "claude-heavy"

    def test_distributed_task(self):
        preset, _reason = select_preset("distributed cache layer", {})
        assert preset == "claude-heavy"

    def test_database_task(self):
        preset, _reason = select_preset("database schema redesign", {})
        assert preset == "claude-heavy"

    def test_authentication_task(self):
        preset, _reason = select_preset("add authentication flow", {})
        assert preset == "claude-heavy"

    def test_redesign_task(self):
        preset, _reason = select_preset("redesign the API layer", {})
        assert preset == "claude-heavy"

    def test_refactor_large_matched_before_refactor(self):
        """'refactor large' is longer and should be sorted first."""
        preset, reason = select_preset("refactor large module", {})
        assert preset == "claude-heavy"
        assert "refactor large" in reason


class TestSelectPresetLanguages:
    """Tests for language-based preset selection."""

    def test_hard_language_increases_score(self):
        preset, reason = select_preset("add a feature", {"languages": ["Rust"]})
        assert preset == "default"  # score 1: one hard language alone is not heavy
        assert "rust" in reason.lower()

    def test_multiple_hard_languages(self):
        _preset, reason = select_preset("add a feature", {"languages": ["Go", "Haskell"]})
        assert "go" in reason.lower()
        assert "haskell" in reason.lower()

    def test_easy_languages_decrease_score(self):
        preset, reason = select_preset("add a feature", {"languages": ["python"]})
        # an all-easy stack scores -1, which maps to budget (score <= -1)
        assert preset == "budget"
        assert "python" in reason

    def test_mixed_languages_no_easy_bonus(self):
        """If languages include non-easy ones, no easy bonus is applied."""
        _preset, reason = select_preset("add a feature", {"languages": ["python", "rust"]})
        assert "rust" in reason.lower()
        assert "well-supported by all models" not in reason

    def test_empty_languages_no_language_reason(self):
        preset, _reason = select_preset("add a feature", {"languages": []})
        assert preset == "default"

    def test_no_languages_key(self):
        preset, _reason = select_preset("add a feature", {})
        assert preset == "default"

    def test_cpp_is_hard(self):
        _preset, reason = select_preset("add a feature", {"languages": ["C++"]})
        assert "c++" in reason.lower()

    def test_scala_is_hard(self):
        _preset, reason = select_preset("add a feature", {"languages": ["Scala"]})
        assert "scala" in reason.lower()

    def test_kotlin_is_hard(self):
        _preset, reason = select_preset("add a feature", {"languages": ["Kotlin"]})
        assert "kotlin" in reason.lower()


class TestSelectPresetProjectSize:
    """Tests for project size affecting preset selection."""

    def test_large_project_increases_score(self):
        _preset, reason = select_preset("add a feature", {"file_count": 100})
        assert "large project" in reason
        assert "100 files" in reason

    def test_small_project_no_size_reason(self):
        _preset, reason = select_preset("add a feature", {"file_count": 10})
        assert "large project" not in reason

    def test_exactly_at_threshold_no_bonus(self):
        """Exactly 50 files does not trigger (threshold is >50)."""
        _preset, reason = select_preset("add a feature", {"file_count": 50})
        assert "large project" not in reason

    def test_just_above_threshold(self):
        _preset, reason = select_preset("add a feature", {"file_count": 51})
        assert "large project" in reason

    def test_missing_file_count_defaults_to_zero(self):
        _preset, reason = select_preset("add a feature", {})
        assert "large project" not in reason


class TestSelectPresetFileReferences:
    """Tests for file reference detection in request text."""

    def test_many_file_mentions_increases_score(self):
        request = "update foo.py bar.js baz.ts qux.go main.rs utils.py extra.json"
        _preset, reason = select_preset(request, {})
        assert "files" in reason

    def test_few_file_mentions_no_effect(self):
        request = "update foo.py and bar.js"
        _preset, reason = select_preset(request, {})
        assert "references" not in reason


class TestSelectPresetCombinations:
    """Tests for combined signals."""

    def test_fast_keyword_plus_easy_language_returns_budget(self):
        preset, _reason = select_preset("simple rename", {"languages": ["python"], "file_count": 5})
        assert preset == "budget"

    def test_heavy_keyword_plus_hard_language_plus_large_project(self):
        preset, _reason = select_preset(
            "security audit of authentication",
            {"languages": ["Rust"], "file_count": 200},
        )
        assert preset == "claude-heavy"

    def test_conflicting_signals_returns_default(self):
        """Simple keyword but hard language can balance out."""
        preset, _reason = select_preset("simple feature", {"languages": ["Rust"]})
        assert preset == "default"

    def test_no_signals_returns_default(self):
        preset, reason = select_preset("add a feature", {})
        assert preset == "default"
        assert reason == "balanced complexity"

    def test_case_insensitivity(self):
        """Request keywords are matched case-insensitively."""
        preset, _ = select_preset("SECURITY audit", {})
        assert preset == "claude-heavy"

    def test_reason_format_with_reasons(self):
        _, reason = select_preset("simple task", {"languages": ["python"]})
        assert "(" in reason and ")" in reason
        assert ";" in reason  # multiple reasons separated by semicolons

    def test_reason_format_without_reasons(self):
        """When no specific signals, reason is just the summary."""
        _, reason = select_preset("add a feature", {})
        assert reason == "balanced complexity"


class TestSelectPresetReturnType:
    """Verify the function always returns the expected structure."""

    def test_returns_tuple(self):
        result = select_preset("anything", {})
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_preset_is_string(self):
        preset, _ = select_preset("anything", {})
        assert isinstance(preset, str)

    def test_reason_is_string(self):
        _, reason = select_preset("anything", {})
        assert isinstance(reason, str)

    def test_preset_is_known_value(self):
        for request in ["simple", "add a feature", "security audit"]:
            preset, _ = select_preset(request, {})
            assert preset in {"budget", "default", "claude-heavy"}
