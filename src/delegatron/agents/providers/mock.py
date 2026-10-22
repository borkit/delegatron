"""Mock provider for testing without API keys.

Returns realistic structured responses for each agent role,
allowing full pipeline testing of orchestration, routing,
termination, and sandbox logic.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from delegatron.agents.providers.base import FINISH_REASON_LENGTH, BaseProvider, ProviderResponse

# Realistic mock responses for each schema type
MOCK_WORK_PLAN = {
    "summary": "Create a FastAPI health endpoint that returns server status and uptime",
    "assumptions": [
        "Python 3.11+ with FastAPI framework",
        "No authentication required for health endpoint",
        "Uptime measured from application start time",
    ],
    "open_questions": [],
    "tasks": [
        {
            "task_id": "t1",
            "title": "Create health endpoint module",
            "description": (
                "Create src/app/health.py with a GET /health endpoint that returns "
                '{"status": "ok", "uptime_seconds": <float>} using FastAPI. '
                "Track application start time at module level."
            ),
            "outputs": ["src/app/health.py"],
            "risk_level": "low",
            "depends_on": [],
        },
        {
            "task_id": "t2",
            "title": "Add health endpoint tests",
            "description": (
                "Create tests/test_health.py with pytest tests using httpx AsyncClient. "
                "Verify: returns 200, has status field, has uptime_seconds field as float."
            ),
            "outputs": ["tests/test_health.py"],
            "risk_level": "low",
            "depends_on": ["t1"],
        },
    ],
    "acceptance": {
        "criteria": [
            "GET /health returns 200 OK",
            "Response contains 'status' field with value 'ok'",
            "Response contains 'uptime_seconds' field as a float >= 0",
            "All tests pass",
        ],
        "tests": ["test_health_returns_200", "test_health_has_status", "test_health_has_uptime"],
    },
    "termination": {
        "done_when": ["All acceptance criteria met", "All tests pass", "Reviewer approves"],
        "stop_limits": {
            "max_iterations": 5,
            "max_cost_usd": 2.0,
            "max_wall_time_minutes": 10,
        },
    },
}

MOCK_PATCH_SET = {
    "status": "ok",
    "diff_unified": (
        "--- /dev/null\n"
        "+++ b/src/app/health.py\n"
        "@@ -0,0 +1,10 @@\n"
        "+import time\n"
        "+from fastapi import APIRouter\n"
        "+\n"
        "+router = APIRouter()\n"
        "+_start_time = time.monotonic()\n"
        "+\n"
        "+@router.get('/health')\n"
        "+async def health_check():\n"
        "+    uptime = time.monotonic() - _start_time\n"
        "+    return {'status': 'ok', 'uptime_seconds': round(uptime, 2)}\n"
    ),
    "rationale": (
        "Created health endpoint with uptime tracking using time.monotonic() for accuracy."
    ),
    "tests_added_or_updated": ["tests/test_health.py"],
    "tests_to_run": ["python -m pytest tests/test_health.py -v"],
    "questions": [],
}

MOCK_REVIEW_APPROVED = {
    "summary": (
        "The health endpoint implementation is clean and correct. "
        "Uses time.monotonic() for reliable uptime measurement, returns proper JSON "
        "structure, and all acceptance criteria are met."
    ),
    "acceptance_met": True,
    "issues": [],
}

MOCK_REVIEW_CHANGES_REQUIRED = {
    "summary": (
        "The health endpoint is mostly correct but has a minor issue: "
        "the response should include a 'version' field for operational monitoring."
    ),
    "acceptance_met": False,
    "issues": [
        {
            "severity": "minor",
            "title": "Missing error handling for edge cases",
            "details": "The endpoint should handle potential errors gracefully.",
            "recommended_fix": "Add a try/except wrapper around the uptime calculation.",
            "evidence": "src/app/health.py:8",
        },
    ],
}

# Director runs on the architect's provider; no rulings keeps the review as-is.
MOCK_ARBITRATION_REPORT: dict[str, Any] = {"resolutions": []}


class MockProvider(BaseProvider):
    """Mock provider for testing without API keys.

    Simulates realistic LLM responses based on the requested schema.
    On iteration 1, the reviewer returns changes_required.
    On iteration 2+, the reviewer approves.

    *truncate_calls* scripts output cut off at the token limit: those calls
    (1-based) return the first half of the response with
    ``finish_reason == "length"`` and ``output_tokens == max_tokens``.
    """

    def __init__(self, role: str = "architect", truncate_calls: Collection[int] = ()) -> None:
        self._role = role
        self._call_count = 0
        self._truncate_calls = frozenset(truncate_calls)

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        self._call_count += 1

        # Determine which mock response based on schema title
        schema_title = (json_schema or {}).get("title", "")

        # Checked before the role branches: the director uses the architect role.
        if schema_title == "ArbitrationReport":
            content = json.dumps(MOCK_ARBITRATION_REPORT)
        elif schema_title == "WorkPlan" or self._role == "architect":
            content = json.dumps(MOCK_WORK_PLAN)
        elif schema_title == "PatchSet" or self._role == "implementer":
            content = json.dumps(MOCK_PATCH_SET)
        elif schema_title == "ReviewReport" or self._role == "reviewer":
            # First call: request changes. Second call: approve.
            if self._call_count <= 1:
                content = json.dumps(MOCK_REVIEW_CHANGES_REQUIRED)
            else:
                content = json.dumps(MOCK_REVIEW_APPROVED)
        else:
            content = json.dumps({"error": "Unknown schema"})

        if self._call_count in self._truncate_calls:
            return ProviderResponse(
                content=content[: len(content) // 2],
                model=f"mock-{self._role}",
                input_tokens=500,
                output_tokens=max_tokens,
                finish_reason=FINISH_REASON_LENGTH,
            )
        return ProviderResponse(
            content=content,
            model=f"mock-{self._role}",
            input_tokens=500,
            output_tokens=300,
            finish_reason="stop",
        )
