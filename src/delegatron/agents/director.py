"""Director agent — binding arbitration of implementer/reviewer conflicts."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel

from delegatron.agents.base import BaseAgent
from delegatron.agents.rendering import bullet_list
from delegatron.schemas.arbitration import ArbitrationReport


class DirectorAgent(BaseAgent):
    """Uses the architect's LLM to settle conflicts the implement/review loop cannot.

    Input keys (all optional): ``request``, ``plan_summary``, ``task`` (current
    task dict), ``acceptance_criteria`` (the whole plan's), ``conflicts``
    (recurring review issues), ``test_passed``, ``test_evidence`` ("passed",
    "failed" or "untested"), ``failing_tests`` (test-output excerpt),
    ``changed_files`` (path -> current content), ``implementer_rationale``.

    Responsibilities:
    - Decide ACCEPT / REVISE / DEFER for each conflict
    - Weigh evidence: failing tests > security > reviewer issues > opinions
    - Give one concrete alternative (file, code or test) for every REVISE
    """

    _context_trim_keys: ClassVar[tuple[str, ...]] = ("changed_files",)

    def _build_user_message(self, input_data: dict[str, Any]) -> str:
        parts: list[str] = []

        request = input_data.get("request", "")
        if request:
            parts.append(f"## Original Request\n\n{request}")

        plan_summary = input_data.get("plan_summary", "")
        if plan_summary:
            parts.append(f"## Plan Summary\n\n{plan_summary}")

        task = input_data.get("task")
        if task:
            parts.append(f"## Current Task\n\n```json\n{json.dumps(task, indent=2)}\n```")

        criteria = input_data.get("acceptance_criteria")
        if criteria:
            parts.append("## Acceptance Criteria (whole plan)\n\n" + bullet_list(criteria))

        conflicts = input_data.get("conflicts")
        if conflicts:
            parts.append(f"## Conflicts\n\n```json\n{json.dumps(conflicts, indent=2)}\n```")

        if "test_passed" in input_data:
            parts.append(f"## Test Result\n\n{_test_status(input_data)}")

        failing_tests = input_data.get("failing_tests", "")
        if failing_tests:
            parts.append(f"## Failing Tests\n\n```text\n{failing_tests}\n```")

        changed_files = input_data.get("changed_files")
        if changed_files:
            file_parts = [
                f"### {path}\n```\n{content}\n```" for path, content in changed_files.items()
            ]
            parts.append("## Current Files\n\n" + "\n\n".join(file_parts))

        rationale = input_data.get("implementer_rationale", "")
        if rationale:
            parts.append(f"## Implementer Rationale\n\n{rationale}")

        return "\n\n".join(parts)

    def _get_output_schema(self) -> dict[str, Any]:
        return ArbitrationReport.model_json_schema()

    def _get_output_model(self) -> type[BaseModel]:
        return ArbitrationReport


def _test_status(input_data: dict[str, Any]) -> str:
    """The test result line: passing, failing, or no test evidence at all (``test_evidence``)."""
    if input_data.get("test_evidence") == "untested":
        return "No tests ran: there is no test evidence either way."
    return "All tests pass." if input_data["test_passed"] else "Tests are failing."
