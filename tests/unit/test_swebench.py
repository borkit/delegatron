"""Unit tests for delegatron.swebench — SWE-bench evaluation adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from delegatron.swebench import (
    DATASETS,
    TIER_THRESHOLDS,
    SWEBenchAdapter,
    SWEBenchInstance,
    SWEBenchReport,
    SWEBenchResult,
    _load_from_jsonl,
    compare_reports,
    load_instances,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_patch(added: int = 0, removed: int = 0) -> str:
    """Build a minimal unified diff with the requested number of +/- lines.

    Includes --- and +++ header lines that must NOT be counted by patch_lines.
    """
    lines = [
        "diff --git a/foo.py b/foo.py",
        "--- a/foo.py",
        "+++ b/foo.py",
        "@@ -1,5 +1,5 @@",
    ]
    for i in range(removed):
        lines.append(f"-old_line_{i}")
    for i in range(added):
        lines.append(f"+new_line_{i}")
    return "\n".join(lines)


def _make_instance_dict(**overrides) -> dict:
    """Return a minimal valid instance dict, with overrides."""
    base = {
        "instance_id": "test/repo__issue-1",
        "repo": "test/repo",
        "base_commit": "abc123",
        "problem_statement": "Fix the bug",
        "patch": _make_patch(added=3, removed=2),
        "test_patch": "",
        "FAIL_TO_PASS": '["tests/test_a.py::test_fail"]',
        "PASS_TO_PASS": '["tests/test_b.py::test_pass"]',
        "version": "1.0",
    }
    base.update(overrides)
    return base


def _make_result(**overrides) -> SWEBenchResult:
    """Return a SWEBenchResult with sensible defaults."""
    defaults = dict(
        instance_id="test/repo__issue-1",
        tier="simple",
        resolved=False,
        status="completed",
        iterations=3,
        cost_usd=0.123456789,
        elapsed_seconds=42.567,
        emcs_score=0.87654,
        patch_generated="--- a/f.py\n+++ b/f.py\n+fix",
    )
    defaults.update(overrides)
    return SWEBenchResult(**defaults)


# ===================================================================
# SWEBenchInstance
# ===================================================================


class TestSWEBenchInstance:
    """Tests for SWEBenchInstance dataclass and from_dict."""

    def test_from_dict_string_json_fields(self) -> None:
        """FAIL_TO_PASS / PASS_TO_PASS supplied as JSON strings."""
        d = _make_instance_dict(
            FAIL_TO_PASS='["t1", "t2"]',
            PASS_TO_PASS='["t3"]',
        )
        inst = SWEBenchInstance.from_dict(d)
        assert inst.fail_to_pass == ["t1", "t2"]
        assert inst.pass_to_pass == ["t3"]

    def test_from_dict_list_fields(self) -> None:
        """FAIL_TO_PASS / PASS_TO_PASS supplied as native lists."""
        d = _make_instance_dict(
            FAIL_TO_PASS=["already", "a", "list"],
            PASS_TO_PASS=["ok"],
        )
        inst = SWEBenchInstance.from_dict(d)
        assert inst.fail_to_pass == ["already", "a", "list"]
        assert inst.pass_to_pass == ["ok"]

    def test_from_dict_empty_json_string(self) -> None:
        """Empty JSON array string '[]' should yield empty list."""
        d = _make_instance_dict(FAIL_TO_PASS="[]", PASS_TO_PASS="[]")
        inst = SWEBenchInstance.from_dict(d)
        assert inst.fail_to_pass == []
        assert inst.pass_to_pass == []

    def test_from_dict_optional_fields_missing(self) -> None:
        """patch, test_patch, version default gracefully when absent."""
        d = {
            "instance_id": "x/y__1",
            "repo": "x/y",
            "base_commit": "deadbeef",
            "problem_statement": "do something",
        }
        inst = SWEBenchInstance.from_dict(d)
        assert inst.patch == ""
        assert inst.test_patch == ""
        assert inst.version == ""
        assert inst.fail_to_pass == []
        assert inst.pass_to_pass == []

    # -- tier property -------------------------------------------------

    @pytest.mark.parametrize(
        "added,removed,expected_tier",
        [
            (3, 2, "simple"),  # 5 changed lines
            (5, 5, "simple"),  # 10 changed lines
            (6, 6, "simple"),  # 12 changed lines (headers excluded)
            (25, 25, "complex"),  # 50 changed lines
            (30, 30, "complex"),  # 60 changed lines
            (100, 100, "feature"),  # 200 changed lines > 200 threshold
        ],
    )
    def test_tier_property(self, added: int, removed: int, expected_tier: str) -> None:
        """tier is classified by changed +/- lines (excluding --- / +++ headers)."""
        patch = _make_patch(added=added, removed=removed)
        inst = SWEBenchInstance.from_dict(_make_instance_dict(patch=patch))
        # The tier property counts only actual change lines, excluding
        # --- and +++ diff header lines.  Same logic as patch_lines.
        total_changed = added + removed  # headers excluded
        expected = "feature"
        for name, thresh in TIER_THRESHOLDS.items():
            if total_changed <= thresh:
                expected = name
                break
        assert inst.tier == expected

    def test_tier_empty_patch(self) -> None:
        """Empty patch should be classified as simple (0 lines)."""
        inst = SWEBenchInstance.from_dict(_make_instance_dict(patch=""))
        assert inst.tier == "simple"

    # -- patch_lines property ------------------------------------------

    def test_patch_lines_excludes_headers(self) -> None:
        """patch_lines must NOT count --- and +++ header lines."""
        patch = _make_patch(added=4, removed=3)
        inst = SWEBenchInstance.from_dict(_make_instance_dict(patch=patch))
        assert inst.patch_lines == 7  # 4 added + 3 removed, not 9

    def test_patch_lines_empty(self) -> None:
        inst = SWEBenchInstance.from_dict(_make_instance_dict(patch=""))
        assert inst.patch_lines == 0

    def test_patch_lines_only_headers(self) -> None:
        """A patch with only --- / +++ lines has 0 changed lines."""
        patch = "--- a/f.py\n+++ b/f.py"
        inst = SWEBenchInstance.from_dict(_make_instance_dict(patch=patch))
        assert inst.patch_lines == 0


# ===================================================================
# SWEBenchResult
# ===================================================================


class TestSWEBenchResult:
    """Tests for SWEBenchResult.to_dict rounding."""

    def test_to_dict_rounding(self) -> None:
        r = _make_result(
            cost_usd=0.123456789,
            elapsed_seconds=42.567891,
            emcs_score=0.87654321,
        )
        d = r.to_dict()
        assert d["cost_usd"] == round(0.123456789, 6)
        assert d["elapsed_seconds"] == round(42.567891, 1)
        assert d["emcs_score"] == round(0.87654321, 3)

    def test_to_dict_contains_all_keys(self) -> None:
        d = _make_result().to_dict()
        expected_keys = {
            "instance_id",
            "tier",
            "resolved",
            "status",
            "iterations",
            "cost_usd",
            "elapsed_seconds",
            "emcs_score",
            "error",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_excludes_patch(self) -> None:
        """patch_generated is intentionally excluded from the serialized dict."""
        d = _make_result(patch_generated="big diff here").to_dict()
        assert "patch_generated" not in d

    def test_to_dict_error_preserved(self) -> None:
        d = _make_result(error="something broke").to_dict()
        assert d["error"] == "something broke"


# ===================================================================
# SWEBenchReport
# ===================================================================


class TestSWEBenchReport:
    def _report_with_results(self, *results: SWEBenchResult) -> SWEBenchReport:
        return SWEBenchReport(
            preset="test",
            split="lite",
            total_instances=len(results),
            results=list(results),
        )

    # -- resolve_rate --------------------------------------------------

    def test_resolve_rate_mixed(self) -> None:
        report = self._report_with_results(
            _make_result(resolved=True),
            _make_result(resolved=False),
            _make_result(resolved=True),
            _make_result(resolved=False),
        )
        assert report.resolve_rate == pytest.approx(0.5)

    def test_resolve_rate_all_resolved(self) -> None:
        report = self._report_with_results(
            _make_result(resolved=True),
            _make_result(resolved=True),
        )
        assert report.resolve_rate == pytest.approx(1.0)

    def test_resolve_rate_none_resolved(self) -> None:
        report = self._report_with_results(
            _make_result(resolved=False),
        )
        assert report.resolve_rate == pytest.approx(0.0)

    def test_resolve_rate_empty(self) -> None:
        report = self._report_with_results()
        assert report.resolve_rate == 0.0

    # -- total_cost / total_time / avg_iterations ----------------------

    def test_total_cost(self) -> None:
        report = self._report_with_results(
            _make_result(cost_usd=1.5),
            _make_result(cost_usd=2.5),
        )
        assert report.total_cost == pytest.approx(4.0)

    def test_total_time(self) -> None:
        report = self._report_with_results(
            _make_result(elapsed_seconds=10.0),
            _make_result(elapsed_seconds=20.0),
        )
        assert report.total_time == pytest.approx(30.0)

    def test_avg_iterations(self) -> None:
        report = self._report_with_results(
            _make_result(iterations=2),
            _make_result(iterations=4),
            _make_result(iterations=6),
        )
        assert report.avg_iterations == pytest.approx(4.0)

    def test_avg_iterations_empty(self) -> None:
        report = self._report_with_results()
        assert report.avg_iterations == 0.0

    # -- by_tier -------------------------------------------------------

    def test_by_tier_breakdown(self) -> None:
        report = self._report_with_results(
            _make_result(tier="simple", resolved=True, cost_usd=1.0, iterations=2),
            _make_result(tier="simple", resolved=False, cost_usd=3.0, iterations=4),
            _make_result(tier="complex", resolved=True, cost_usd=5.0, iterations=5),
        )
        tiers = report.by_tier()
        assert set(tiers.keys()) == {"simple", "complex"}
        assert tiers["simple"]["total"] == 2
        assert tiers["simple"]["resolved"] == 1
        assert tiers["simple"]["resolve_rate"] == pytest.approx(0.5)
        assert tiers["simple"]["avg_cost"] == pytest.approx(2.0)
        assert tiers["simple"]["avg_iterations"] == pytest.approx(3.0)
        assert tiers["complex"]["total"] == 1
        assert tiers["complex"]["resolved"] == 1
        assert tiers["complex"]["resolve_rate"] == pytest.approx(1.0)

    def test_by_tier_empty(self) -> None:
        report = self._report_with_results()
        assert report.by_tier() == {}

    # -- to_dict -------------------------------------------------------

    def test_to_dict_full(self) -> None:
        report = self._report_with_results(
            _make_result(resolved=True, cost_usd=1.0, elapsed_seconds=10.0, iterations=2),
        )
        report.started_at = "2026-01-01T00:00:00"
        report.completed_at = "2026-01-01T00:01:00"

        d = report.to_dict()
        assert d["preset"] == "test"
        assert d["split"] == "lite"
        assert d["total_instances"] == 1
        assert d["completed"] == 1
        assert d["resolve_rate"] == pytest.approx(1.0)
        assert d["total_cost_usd"] == pytest.approx(1.0)
        assert d["total_time_seconds"] == pytest.approx(10.0)
        assert d["avg_iterations"] == pytest.approx(2.0)
        assert "by_tier" in d
        assert isinstance(d["results"], list)
        assert len(d["results"]) == 1
        assert d["started_at"] == "2026-01-01T00:00:00"
        assert d["completed_at"] == "2026-01-01T00:01:00"

    def test_to_dict_empty_results(self) -> None:
        report = self._report_with_results()
        d = report.to_dict()
        assert d["completed"] == 0
        assert d["resolve_rate"] == 0.0
        assert d["results"] == []
        assert d["by_tier"] == {}


# ===================================================================
# _load_from_jsonl
# ===================================================================


class TestLoadFromJsonl:
    def _write_jsonl(self, path: Path, instances: list[dict]) -> None:
        with open(path, "w") as f:
            for inst in instances:
                f.write(json.dumps(inst) + "\n")

    def test_basic_load(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "data.jsonl"
        self._write_jsonl(
            jsonl,
            [
                _make_instance_dict(instance_id="r/a__1"),
                _make_instance_dict(instance_id="r/a__2"),
                _make_instance_dict(instance_id="r/a__3"),
            ],
        )
        instances = _load_from_jsonl(jsonl, limit=None, tier=None)
        assert len(instances) == 3
        assert instances[0].instance_id == "r/a__1"

    def test_limit(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "data.jsonl"
        self._write_jsonl(jsonl, [_make_instance_dict(instance_id=f"r/a__{i}") for i in range(10)])
        instances = _load_from_jsonl(jsonl, limit=3, tier=None)
        assert len(instances) == 3

    def test_tier_filter(self, tmp_path: Path) -> None:
        """Only instances matching the requested tier are returned."""
        jsonl = tmp_path / "data.jsonl"
        # simple: <=10 lines (including --- and +++ headers = 2 extra)
        simple_patch = _make_patch(added=3, removed=2)  # 5 + 2 = 7 -> simple
        complex_patch = _make_patch(added=40, removed=40)  # 80 + 2 = 82 -> complex
        self._write_jsonl(
            jsonl,
            [
                _make_instance_dict(instance_id="r/a__1", patch=simple_patch),
                _make_instance_dict(instance_id="r/a__2", patch=complex_patch),
                _make_instance_dict(instance_id="r/a__3", patch=simple_patch),
            ],
        )
        instances = _load_from_jsonl(jsonl, limit=None, tier="simple")
        assert len(instances) == 2
        assert all(inst.tier == "simple" for inst in instances)

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "data.jsonl"
        content = (
            json.dumps(_make_instance_dict(instance_id="r/a__1"))
            + "\n"
            + "\n"
            + "   \n"
            + json.dumps(_make_instance_dict(instance_id="r/a__2"))
            + "\n"
        )
        jsonl.write_text(content)
        instances = _load_from_jsonl(jsonl, limit=None, tier=None)
        assert len(instances) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "empty.jsonl"
        jsonl.write_text("")
        instances = _load_from_jsonl(jsonl, limit=None, tier=None)
        assert instances == []


# ===================================================================
# compare_reports
# ===================================================================


class TestCompareReports:
    def test_compare_two_reports(self, tmp_path: Path) -> None:
        r1 = SWEBenchReport(
            preset="fast",
            split="lite",
            total_instances=2,
            results=[
                _make_result(
                    tier="simple", resolved=True, cost_usd=1.0, iterations=2, elapsed_seconds=10.0
                ),
            ],
        )
        r2 = SWEBenchReport(
            preset="quality",
            split="lite",
            total_instances=2,
            results=[
                _make_result(
                    tier="simple", resolved=False, cost_usd=3.0, iterations=5, elapsed_seconds=30.0
                ),
            ],
        )
        p1 = tmp_path / "r1.json"
        p2 = tmp_path / "r2.json"
        p1.write_text(json.dumps(r1.to_dict(), indent=2))
        p2.write_text(json.dumps(r2.to_dict(), indent=2))

        cmp = compare_reports(p1, p2)

        assert len(cmp["configs"]) == 2
        assert cmp["configs"][0]["preset"] == "fast"
        assert cmp["configs"][1]["preset"] == "quality"

        assert len(cmp["overall"]) == 2
        assert cmp["overall"][0]["resolve_rate"] == pytest.approx(1.0)
        assert cmp["overall"][1]["resolve_rate"] == pytest.approx(0.0)

        assert "simple" in cmp["by_tier"]
        assert len(cmp["by_tier"]["simple"]) == 2

    def test_compare_single_report(self, tmp_path: Path) -> None:
        r1 = SWEBenchReport(
            preset="solo",
            split="verified",
            total_instances=1,
            results=[_make_result(tier="moderate", resolved=True)],
        )
        p1 = tmp_path / "r1.json"
        p1.write_text(json.dumps(r1.to_dict(), indent=2))

        cmp = compare_reports(p1)
        assert len(cmp["configs"]) == 1
        assert "moderate" in cmp["by_tier"]


# ===================================================================
# load_instances (dispatches to jsonl or huggingface)
# ===================================================================


class TestLoadInstances:
    def test_delegates_to_jsonl(self, tmp_path: Path) -> None:
        """When jsonl_path is given, load_instances uses _load_from_jsonl."""
        jsonl = tmp_path / "data.jsonl"
        jsonl.write_text(json.dumps(_make_instance_dict()) + "\n")
        instances = load_instances(jsonl_path=jsonl)
        assert len(instances) == 1

    @patch("delegatron.swebench._load_from_huggingface")
    def test_delegates_to_huggingface(self, mock_hf: MagicMock) -> None:
        """Without jsonl_path, load_instances calls _load_from_huggingface."""
        mock_hf.return_value = [
            SWEBenchInstance.from_dict(_make_instance_dict()),
        ]
        instances = load_instances(split="lite", limit=5, tier="simple")
        mock_hf.assert_called_once_with("lite", 5, "simple")
        assert len(instances) == 1


# ===================================================================
# SWEBenchAdapter.__init__
# ===================================================================


class TestSWEBenchAdapterInit:
    def test_defaults(self) -> None:
        adapter = SWEBenchAdapter()
        assert adapter.preset == "default"
        assert adapter.work_dir == Path("swebench-workdir")
        assert adapter.artifacts_dir == Path("swebench-runs")
        assert adapter.max_iterations == 5
        assert adapter.max_cost_usd == 5.0

    def test_custom_values(self, tmp_path: Path) -> None:
        work = tmp_path / "work"
        arts = tmp_path / "arts"
        adapter = SWEBenchAdapter(
            preset="budget",
            work_dir=work,
            artifacts_dir=arts,
            max_iterations=3,
            max_cost_usd=1.5,
        )
        assert adapter.preset == "budget"
        assert adapter.work_dir == work
        assert adapter.artifacts_dir == arts
        assert adapter.max_iterations == 3
        assert adapter.max_cost_usd == 1.5


# ===================================================================
# Module-level constants
# ===================================================================


class TestConstants:
    def test_datasets_has_expected_splits(self) -> None:
        assert set(DATASETS.keys()) == {"lite", "verified", "full"}

    def test_tier_thresholds_ordered(self) -> None:
        values = list(TIER_THRESHOLDS.values())
        assert values == sorted(values), "Tier thresholds must be in ascending order"

    def test_tier_thresholds_has_expected_keys(self) -> None:
        assert set(TIER_THRESHOLDS.keys()) == {"simple", "moderate", "complex", "feature"}


class TestRunInstanceWiring:
    @pytest.mark.asyncio
    async def test_fresh_run_on_the_base_commit_and_files_patches_are_evaluated(
        self, tmp_path: Path
    ) -> None:
        """Each attempt gets its own run branch off the base commit; the evaluated
        patch is what the run committed, also for ``files``-only patches."""
        import subprocess

        from delegatron.config import Config

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        repo = tmp_path / "repo"
        repo.mkdir()
        git("init", "-q", "-b", "main")
        (repo / "mod.py").write_bytes(b"x = 1\n")
        git("add", "mod.py")
        git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        git("checkout", "-q", "--detach", base)
        created: list[dict] = []

        class _FakePipeline:
            def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
                created.append(kwargs)

            async def execute(self, request: str) -> dict:
                git("checkout", "-q", "-b", f"delegatron/run-{created[-1]['run_id']}")
                (repo / "mod.py").write_bytes(b"x = 2\n")
                git("-c", "commit.gpgsign=false", "commit", "-q", "-am", "fix")
                return {
                    "status": "approved",
                    "iteration": 1,
                    "current_patch": {"files": {"mod.py": "x = 2\n"}, "diff_unified": ""},
                }

        instance = SWEBenchInstance.from_dict(_make_instance_dict(base_commit=base))
        adapter = SWEBenchAdapter(preset="mock", work_dir=tmp_path, artifacts_dir=tmp_path / "a")
        evaluated: list[str] = []

        def evaluate(_instance, patch_text, _repo_dir):  # type: ignore[no-untyped-def]
            evaluated.append(patch_text)
            return True

        with (
            patch("delegatron.swebench._setup_repo", return_value=repo),
            patch("delegatron.swebench.load_config", return_value=Config()),
            patch("delegatron.swebench.Pipeline", _FakePipeline),
            patch("delegatron.swebench._evaluate_patch", side_effect=evaluate),
        ):
            first = await adapter._run_instance(instance)
            git("checkout", "-q", "--detach", base)
            await adapter._run_instance(instance)

        assert first.resolved is True
        assert created[0]["run_id"] != created[1]["run_id"]
        assert all(kw["run_id"].startswith("swe-test-repo__issue-1-") for kw in created)
        assert [kw["base_branch"] for kw in created] == [base, base]
        assert "+x = 2" in evaluated[0]
