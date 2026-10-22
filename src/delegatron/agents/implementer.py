"""GPT implementer agent — generates code patches from task specs."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from delegatron.agents.base import BaseAgent
from delegatron.agents.rendering import (
    bullet_list,
    failure_list,
    fenced,
    files_block,
    json_block,
    removed_tests_section,
    report_excerpt,
    result_line,
)
from delegatron.schemas.patch_set import PatchSet

# Guidance sections rendered last, after the evidence and the code: (input key, heading).
_GUIDANCE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("director_guidance", "Director Guidance"),
    ("security_findings", "Security Findings"),
    ("import_warnings", "Import Warnings (advisory)"),
    ("review_feedback", "Reviewer Notes"),
    ("constraints", "Constraints"),
)


class ImplementerAgent(BaseAgent):
    """Uses the implementer LLM to generate code changes as structured PatchSets.

    Responsibilities:
    - Generate code patches following the TaskSpec exactly
    - Add/update tests when behavior changes
    - Signal "blocked" status with questions when clarification needed
    """

    # Files the context guard drops are still listed under "Files Not Shown".
    _dropped_files_listing: ClassVar[dict[str, str]] = {"existing_files": "omitted_files"}

    def _build_user_message(self, input_data: dict[str, Any]) -> str:
        parts: list[str] = []

        # Original user request (may contain attached file contents)
        request = input_data.get("request", "")
        if request:
            parts.append(f"## Original Request\n\n{request}")

        # Task specification
        task = input_data.get("task")
        if task:
            parts.append(f"## Task Specification\n\n{json_block(task)}")

        # Full plan context
        plan_summary = input_data.get("plan_summary", "")
        if plan_summary:
            parts.append(f"## Plan Summary\n\n{plan_summary}")

        # Project context
        project_info = input_data.get("project_info")
        if project_info:
            parts.append(f"## Project Context\n\n{json_block(project_info)}")

        # Framework-specific hints (Vitest vs Jest, available deps, etc).
        # Built from package.json / pyproject.toml at pipeline start — keep
        # directly under the project context so the model reads them before
        # generating imports.
        framework_hints = input_data.get("framework_hints")
        if framework_hints:
            parts.append("## Framework Hints\n\n" + bullet_list(framework_hints))

        # Failing tests are the strongest evidence, so they come before the
        # code.  Callers that predate ``failing_tests`` pass the raw report.
        # The counts line comes from ``test_counts`` (or the report).
        failing_tests = input_data.get("failing_tests")
        test_report = input_data.get("test_report")
        if failing_tests is None and test_report and not test_report.get("passed"):
            failing_tests = report_excerpt(test_report)
        if failing_tests:
            counts = input_data.get("test_counts") or test_report
            summary = f"{result_line(counts)}\n\n" if counts else ""
            parts.append(f"## Failing Tests\n\n{summary}{fenced(failing_tests, 'text')}")

        baseline_failures = input_data.get("baseline_failures")
        if baseline_failures:
            parts.append("## Pre-existing Failures\n\n" + failure_list(baseline_failures))

        flaky_tests = input_data.get("flaky_tests")
        if flaky_tests:
            parts.append("## Flaky Tests\n\n" + bullet_list(flaky_tests))

        removed_tests = input_data.get("removed_tests")
        if removed_tests:
            parts.append(removed_tests_section(removed_tests))

        # Current contents of the relevant files
        # An excerpted file's heading names its on-disk size (``partial_files``).
        existing_files = input_data.get("existing_files")
        if existing_files:
            partial = input_data.get("partial_files") or {}
            parts.append("## Current Files\n\n" + files_block(existing_files, partial))

        # Relevant files that are not shown: too large for the budget, or binary
        omitted = input_data.get("omitted_files") or {}
        binary = input_data.get("binary_files") or {}
        not_shown = [
            *(f"{path} ({size} bytes)" for path, size in omitted.items()),
            *(f"{path} ({size} bytes, binary)" for path, size in binary.items()),
        ]
        if not_shown:
            parts.append("## Files Not Shown\n\n" + bullet_list(not_shown))

        # Structural map (signatures) of the rest of the repository
        repo_map = input_data.get("repo_map")
        if repo_map:
            parts.append(f"## Repository Map\n\n{fenced(repo_map)}")

        # Outcome of the previous patch (e.g. it changed no files)
        previous_attempt = input_data.get("previous_attempt")
        if previous_attempt:
            parts.append(f"## Previous Attempt\n\n{json_block(previous_attempt)}")

        for key, heading in _GUIDANCE_SECTIONS:
            value = input_data.get(key)
            if value:
                parts.append(f"## {heading}\n\n{json_block(value)}")

        return "\n\n".join(parts)

    def _get_output_schema(self) -> dict[str, Any]:
        return PatchSet.model_json_schema()

    def _get_output_model(self) -> type[BaseModel]:
        return PatchSet
