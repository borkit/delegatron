"""Tests for the director agent, its ArbitrationReport schema and prompt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from delegatron.agents.director import DirectorAgent
from delegatron.agents.providers.base import BaseProvider, ProviderResponse
from delegatron.schemas.arbitration import ArbitrationReport, Resolution

REPO_ROOT = Path(__file__).resolve().parents[2]


class RecordingProvider(BaseProvider):
    """Returns a fixed response and records the last request."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        self.calls.append(
            {"system_prompt": system_prompt, "user_message": user_message, "schema": json_schema}
        )
        return ProviderResponse(content=self._content, model="mock-director")


def _director(content: str = '{"resolutions": []}') -> DirectorAgent:
    return DirectorAgent(provider=RecordingProvider(content), system_prompt="director")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestResolution:
    @pytest.mark.parametrize("raw", ["revise", "Revise", " REVISE "])
    def test_decision_is_case_insensitive(self, raw: str) -> None:
        assert Resolution(title="t", decision=raw).decision == "REVISE"

    @pytest.mark.parametrize("raw", ["REJECT", "approve", "", None, 1])
    def test_unknown_decision_fails_closed_to_revise(self, raw: Any) -> None:
        """An unreadable ruling keeps the conflict open instead of failing the call."""
        assert Resolution(title="t", decision=raw).decision == "REVISE"

    def test_missing_decision_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Resolution(title="t")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("Test", "test"), (" code ", "code"), ("Architecture", "architecture")],
    )
    def test_fix_target_is_normalised(self, raw: str, expected: str) -> None:
        assert Resolution(title="t", decision="REVISE", fix_target=raw).fix_target == expected

    @pytest.mark.parametrize("raw", ["both", "plan", "", 3])
    def test_invalid_fix_target_becomes_none(self, raw: Any) -> None:
        assert Resolution(title="t", decision="REVISE", fix_target=raw).fix_target is None

    def test_defaults(self) -> None:
        resolution = Resolution(title="t", decision="ACCEPT")
        assert resolution.reasoning == ""
        assert resolution.alternative == ""
        assert resolution.fix_target is None
        assert resolution.fix_file is None
        assert ArbitrationReport().resolutions == []

    def test_json_schema_file_matches_model(self) -> None:
        path = REPO_ROOT / "schemas" / "arbitration_report.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["title"] == "ArbitrationReport"
        assert set(schema["properties"]) == set(ArbitrationReport.model_fields)
        items = schema["properties"]["resolutions"]["items"]
        assert set(items["properties"]) == set(Resolution.model_fields)
        assert items["properties"]["decision"]["enum"] == ["ACCEPT", "REVISE", "DEFER"]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class TestDirectorMessage:
    def test_renders_every_section_in_order(self) -> None:
        msg = _director()._build_user_message(
            {
                "request": "Simulate a card deal",
                "plan_summary": "Deck module plus a deal script",
                "task": {"task_id": "t3", "title": "Deal script"},
                "conflicts": [{"title": "Wrong print count", "severity": "major"}],
                "test_passed": False,
                "failing_tests": 'E       AssertionError: 14 != 12\n  File "x.py", line 3',
                "changed_files": {"simulate_deal.py": "print('hand')"},
                "implementer_rationale": "Added a print per card",
            }
        )
        headers = [
            "## Original Request",
            "## Plan Summary",
            "## Current Task",
            "## Conflicts",
            "## Test Result",
            "## Failing Tests",
            "## Current Files",
            "## Implementer Rationale",
        ]
        positions = [msg.index(header) for header in headers]
        assert positions == sorted(positions)
        assert "Tests are failing." in msg
        assert "Wrong print count" in msg
        assert "### simulate_deal.py\n```\nprint('hand')\n```" in msg
        # Test output is real text, not a JSON-escaped string
        assert 'AssertionError: 14 != 12\n  File "x.py", line 3' in msg

    def test_passing_tests_label(self) -> None:
        msg = _director()._build_user_message({"test_passed": True})
        assert msg == "## Test Result\n\nAll tests pass."

    def test_omits_empty_sections_and_instruction_prose(self) -> None:
        msg = _director()._build_user_message(
            {"request": "r", "conflicts": [], "changed_files": {}, "instruction": "Decide."}
        )
        assert msg == "## Original Request\n\nr"

    def test_trims_changed_files_first(self) -> None:
        assert DirectorAgent._context_trim_keys == ("changed_files",)


class TestDirectorInvoke:
    async def test_returns_validated_resolutions(self) -> None:
        content = json.dumps(
            {
                "resolutions": [
                    {
                        "title": "Wrong print count",
                        "decision": "revise",
                        "reasoning": "The task says print each hand once",
                        "alternative": "In tests/test_deal.py, expect 2 prints",
                        "fix_target": "TEST",
                        "fix_file": "tests/test_deal.py",
                    }
                ]
            }
        )
        agent = _director(content)
        result = await agent.invoke({"conflicts": [{"title": "Wrong print count"}]})

        assert result["resolutions"] == [
            {
                "title": "Wrong print count",
                "decision": "REVISE",
                "reasoning": "The task says print each hand once",
                "alternative": "In tests/test_deal.py, expect 2 prints",
                "fix_target": "test",
                "fix_file": "tests/test_deal.py",
            }
        ]
        provider = agent._provider
        assert isinstance(provider, RecordingProvider)
        assert provider.calls[0]["schema"]["title"] == "ArbitrationReport"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def test_director_prompt_v1_covers_decisions_and_output_fields() -> None:
    prompt = (REPO_ROOT / "prompts" / "director" / "v1.md").read_text(encoding="utf-8")
    for term in ("ACCEPT", "REVISE", "DEFER", '"resolutions"', '"fix_target"', '"fix_file"'):
        assert term in prompt


class TestDirectorEvidence:
    def test_renders_the_plans_criteria_after_the_task(self) -> None:
        msg = _director()._build_user_message(
            {
                "task": {"task_id": "t1"},
                "acceptance_criteria": ["Deck has 52 cards", "Deal prints five cards"],
                "conflicts": [{"title": "x"}],
            }
        )

        assert "## Acceptance Criteria (whole plan)\n\n- Deck has 52 cards" in msg
        assert msg.index("## Current Task") < msg.index("## Acceptance Criteria")
        assert msg.index("## Acceptance Criteria") < msg.index("## Conflicts")

    def test_untested_run_is_not_called_failing(self) -> None:
        msg = _director()._build_user_message({"test_passed": False, "test_evidence": "untested"})

        assert "No tests ran" in msg
        assert "Tests are failing." not in msg
