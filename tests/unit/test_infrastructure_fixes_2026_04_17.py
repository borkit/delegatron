"""Unit tests for infrastructure fixes landed 2026-04-17.

Covers three regressions that each silently broke a language pipeline or
feature:

1. Maven install command must pre-warm the surefire provider, not just
   resolve declared deps.  Test name must match surefire's include pattern.
2. Parallel-task gate should fire when ANY wave has >1 tasks, not only
   when multiple sequential waves exist.
3. DOCKER_IMAGES has java/ruby/php entries (previously unmapped — fell
   through to the python default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from delegatron.factories.sandbox_factory import (
    get_install_commands,
    select_docker_image,
)


class TestMavenInstallCommandPrewarmsSurefire:
    """``mvn dependency:go-offline`` alone misses surefire-junit-platform.

    Surefire lazy-loads its test-framework provider based on what's on the
    test classpath, so the install phase must actually invoke surefire
    via a compiled test class. The compiled class name has to match
    surefire's default include pattern (*Test.java) or surefire skips
    test execution entirely, which skips provider resolution.
    """

    def _project(self, package_manager: str) -> object:
        info = MagicMock()
        info.package_manager = package_manager
        return info

    def test_maven_install_includes_go_offline_and_prewarm(self, tmp_path: Path) -> None:
        cmds = get_install_commands(self._project("maven"), tmp_path)

        # 1. Deps + plugins via go-offline
        assert any("dependency:go-offline" in c for c in cmds), cmds
        # 2. Prewarm invokes surefire via a real test class
        prewarm = next((c for c in cmds if "mvn -B -q test" in c), None)
        assert prewarm is not None, "expected a Maven prewarm test command"
        # 3. The class file name matches surefire's default include (*Test.java)
        assert "DelegatronPrewarmTest.java" in prewarm
        # 4. Cleans up after itself so the dummy doesn't leak into the agent's run
        assert "rm -f src/test/java/DelegatronPrewarmTest.java" in prewarm

    def test_non_maven_projects_get_no_prewarm(self, tmp_path: Path) -> None:
        for pm in ("npm", "cargo", "go", "bundler"):
            cmds = get_install_commands(self._project(pm), tmp_path)
            joined = " ".join(cmds)
            assert "DelegatronPrewarmTest" not in joined


class TestLanguageImageMappings:
    """Java, Ruby, PHP must resolve to real images — not the Python default."""

    def test_java_gets_maven_image(self) -> None:
        assert select_docker_image("java") == "maven:3.9-eclipse-temurin-21"

    def test_kotlin_also_uses_maven_image(self) -> None:
        assert select_docker_image("kotlin") == "maven:3.9-eclipse-temurin-21"

    def test_ruby_gets_ruby_slim(self) -> None:
        assert select_docker_image("ruby") == "ruby:3.3-slim"

    def test_php_gets_composer_image(self) -> None:
        # Must bundle composer; php:*-cli alone lacks it.
        assert select_docker_image("php") == "composer:2"


class TestFanoutWaveGate:
    """Parallel mode fans out every ready task, not only multi-stage plans.

    The old gate checked ``len(waves) > 1`` (missing all-independent plans);
    the graph now picks the current task's ready siblings itself
    (``Pipeline._parallel_siblings``) on the task's first attempt.
    """

    @staticmethod
    def _siblings(tasks: list[dict[str, Any]], **state: Any) -> list[str]:
        from delegatron.orchestrator.graph import Pipeline

        full = {"all_tasks": tasks, "current_task": tasks[0], "_task_iteration": 0, **state}
        return [t["task_id"] for t in Pipeline._parallel_siblings(cast(Any, full))]

    def test_independent_tasks_fan_out(self) -> None:
        tasks = [{"task_id": "t1"}, {"task_id": "t2"}, {"task_id": "t3"}]
        assert self._siblings(tasks) == ["t2", "t3"]

    def test_single_task_has_no_siblings(self) -> None:
        assert self._siblings([{"task_id": "t1"}]) == []

    def test_a_dependent_task_waits_for_its_dependency(self) -> None:
        tasks = [
            {"task_id": "s"},
            {"task_id": "a", "depends_on": ["s"]},
            {"task_id": "b"},
        ]
        assert self._siblings(tasks) == ["b"]

    def test_retries_run_sequentially(self) -> None:
        """Fan-out is first-attempt only; retries follow the reviewer task by task."""
        tasks = [{"task_id": "t1"}, {"task_id": "t2"}]
        assert self._siblings(tasks, _task_iteration=1) == []

    def test_a_generated_patch_is_not_generated_again(self) -> None:
        tasks = [{"task_id": "t1"}, {"task_id": "t2"}]
        assert self._siblings(tasks, _parallel_patches={"t2": {"patch": {}}}) == []
