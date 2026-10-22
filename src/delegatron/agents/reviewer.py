"""Gemini reviewer agent — reviews code and produces structured ReviewReports."""

from __future__ import annotations

from collections.abc import Mapping
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
from delegatron.schemas.review_report import ReviewReport


class ReviewerAgent(BaseAgent):
    """Uses the reviewer LLM to review diffs against acceptance criteria.

    Responsibilities:
    - Assess correctness, security, performance, and style
    - Categorize issues by severity (blocker/major/minor/nit)
    - Verify acceptance criteria are met
    - Provide actionable fix recommendations
    """

    # The diff already shows what changed, so whole files are trimmed first.
    _context_trim_keys: ClassVar[tuple[str, ...]] = ("related_files", "changed_files")

    def _build_user_message(self, input_data: dict[str, Any]) -> str:
        parts: list[str] = []

        # Original request for context
        request = input_data.get("request", "")
        if request:
            parts.append(f"## Original Request\n\n{request}")

        # The task under review, and the planned tasks that are out of scope
        task = input_data.get("task")
        if task:
            parts.append(f"## Current Task\n\n{json_block(task)}")

        later_tasks = input_data.get("later_tasks")
        if later_tasks:
            parts.append("## Later Tasks (out of scope)\n\n" + bullet_list(later_tasks))

        # The plan's acceptance criteria, covering every task of the plan
        acceptance = input_data.get("acceptance")
        if acceptance:
            parts.append(f"## Overall Acceptance Criteria (whole plan)\n\n{json_block(acceptance)}")

        # Project rules the change must respect
        constraints = input_data.get("constraints")
        if constraints:
            parts.append(f"## Constraints\n\n{json_block(constraints)}")

        # The changes to review
        diff = input_data.get("diff", "")
        if input_data.get("patch_noop"):
            parts.append("## Code Changes\n\nThis attempt changed no files.")
        elif diff:
            parts.append(f"## Code Changes (Unified Diff)\n\n{fenced(diff, 'diff')}")

        changed_files = input_data.get("changed_files")
        if changed_files:
            parts.append("## Changed Files\n\n" + files_block(changed_files))

        related_files = input_data.get("related_files")
        if related_files:
            parts.append("## Related Files (unchanged)\n\n" + files_block(related_files))

        # Test report
        test_report = input_data.get("test_report")
        if test_report:
            parts.append(_render_test_report(test_report))

        baseline_failures = input_data.get("baseline_failures")
        if baseline_failures:
            parts.append("## Pre-existing Failures\n\n" + failure_list(baseline_failures))

        # Accepted tests of earlier tasks that this attempt deleted
        removed_tests = input_data.get("removed_tests")
        if removed_tests:
            parts.append(removed_tests_section(removed_tests))

        # Findings of the orchestrator's security scan of this patch
        security_findings = input_data.get("security_findings")
        if security_findings:
            parts.append(f"## Security Findings\n\n{json_block(security_findings)}")

        import_warnings = input_data.get("import_warnings")
        if import_warnings:
            parts.append(f"## Import Warnings (advisory)\n\n{json_block(import_warnings)}")

        # Iteration context — only on a retry that has issues to check.  The
        # attempt number counts attempts on the current task, never the run.
        previous_issues = input_data.get("previous_issues")
        if previous_issues:
            attempt = input_data.get("task_attempt")
            lead = f"This is attempt {attempt} of the current task. " if attempt else ""
            parts.append(
                f"## Iteration Context\n\n"
                f"{lead}Issues from the previous review:\n\n{json_block(previous_issues)}"
            )

        return "\n\n".join(parts)

    def _get_output_schema(self) -> dict[str, Any]:
        return ReviewReport.model_json_schema()

    def _get_output_model(self) -> type[BaseModel]:
        return ReviewReport


def _render_test_report(report: Mapping[str, Any]) -> str:
    """Render a sandbox report as readable text.

    Verdict, targeted run, output, flaky tests, lint advisories and script runs.
    """
    lines = [result_line(report)]
    if report.get("skipped_reason"):
        lines.append(f"skipped_reason: {report['skipped_reason']}")
    if report.get("synthetic_tests"):
        lines.append("synthetic_tests: only the orchestrator's smoke tests ran")
    targeted = report.get("targeted")
    if targeted:
        status = "PASSED" if targeted.get("passed") else "FAILED"
        failing = ", ".join(targeted.get("failing_ids") or [])
        lines.append(f"Targeted tests (this task's tests_to_run): {status}")
        if failing:
            lines.append(f"Targeted failures: {failing}")
    if report.get("flaky_tests"):
        lines.append("Flaky tests (passed and failed with no code change):")
        lines.append(bullet_list(report["flaky_tests"]))
    if report.get("lint_warnings"):
        lines.append("Lint warnings (advisory):")
        lines.append(bullet_list(report["lint_warnings"]))
    sections = ["## Test Results\n\n" + "\n".join(lines)]

    output = report_excerpt(report)
    if output:
        sections.append(f"### Test Output\n\n{fenced(output, 'text')}")

    # The task's own tests ran first; when they failed, show how.
    targeted_tail = (targeted or {}).get("stdout_tail") or ""
    if targeted and not targeted.get("passed") and targeted_tail.strip():
        sections.append(f"### Targeted Test Output\n\n{fenced(targeted_tail, 'text')}")

    for run in report.get("entrypoint_runs") or []:
        sections.append(_render_entrypoint_run(run))
    return "\n\n".join(sections)


def _render_entrypoint_run(run: Mapping[str, Any]) -> str:
    """Render one ``python <file>`` smoke run with its exit status and output."""
    returncode = run.get("returncode")
    if run.get("timed_out"):
        status = "timed out"
    elif returncode is None:
        status = "no exit code"
    else:
        status = f"exit code {returncode}"
    stdout = run.get("stdout_head") or ""
    lines = [
        f"### Entrypoint Run: python {run.get('file', '')}",
        "",
        f"Status: {status}",
        "",
        "stdout:" if stdout else "stdout: (empty)",
    ]
    if stdout:
        lines.append(fenced(stdout, "text"))
    stderr = run.get("stderr_tail") or ""
    if stderr:
        lines.extend(["", "stderr:", fenced(stderr, "text")])
    return "\n".join(lines)
