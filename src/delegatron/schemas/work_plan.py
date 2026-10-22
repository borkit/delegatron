"""WorkPlan schema — Claude architect output.

The models repair common slips in model output instead of rejecting the plan:
integer ids become strings, ``null`` or a single string becomes a list, and
fields the orchestrator does not depend on (``risk_level``, ``acceptance.tests``,
``termination``) are filled in when missing.  The fillers run before
validation, so those fields stay required in the JSON schema the model sees.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator, model_validator


def _as_str_list(value: Any) -> Any:
    """``None`` → ``[]``, a string → ``[string]``, integer items → strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [_as_str(item) for item in value]
    return value


def _as_str(value: Any) -> Any:
    """Integers (task ids such as ``1``) become strings; anything else is kept."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


def _with_defaults(data: Any, defaults: dict[str, Any]) -> Any:
    """Return *data* with each missing or ``null`` key in *defaults* filled in."""
    if not isinstance(data, dict):
        return data
    missing = {key: value for key, value in defaults.items() if data.get(key) is None}
    return {**data, **missing} if missing else data


class Task(BaseModel):
    """A single implementable task within the work plan."""

    task_id: str
    title: str
    description: str
    outputs: list[str]
    risk_level: str  # low | medium | high
    depends_on: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _fill_risk_level(cls, data: Any) -> Any:
        return _with_defaults(data, {"risk_level": "medium"})

    @field_validator("task_id", mode="before")
    @classmethod
    def _coerce_task_id(cls, value: Any) -> Any:
        return _as_str(value)

    @field_validator("outputs", "depends_on", mode="before")
    @classmethod
    def _coerce_lists(cls, value: Any) -> Any:
        return _as_str_list(value)


class AcceptanceCriteria(BaseModel):
    """Measurable criteria and tests for completion."""

    criteria: list[str]
    tests: list[str]

    @model_validator(mode="before")
    @classmethod
    def _fill_tests(cls, data: Any) -> Any:
        return _with_defaults(data, {"tests": []})

    @field_validator("criteria", "tests", mode="before")
    @classmethod
    def _coerce_lists(cls, value: Any) -> Any:
        return _as_str_list(value)


class StopLimits(BaseModel):
    """Budget guards for the pipeline run."""

    max_iterations: int = 5
    max_cost_usd: float = 5.0
    max_wall_time_minutes: int = 60


class TerminationPolicy(BaseModel):
    """When to stop iterating."""

    done_when: list[str]
    stop_limits: StopLimits = StopLimits()

    @model_validator(mode="before")
    @classmethod
    def _fill_done_when(cls, data: Any) -> Any:
        return _with_defaults(data, {"done_when": []})

    @field_validator("done_when", mode="before")
    @classmethod
    def _coerce_done_when(cls, value: Any) -> Any:
        return _as_str_list(value)


class WorkPlan(BaseModel):
    """Structured output from the Claude architect agent.

    Represents the full decomposition of a user request into
    implementable tasks with acceptance criteria and termination rules.
    """

    summary: str
    assumptions: list[str] = []
    open_questions: list[str] = []
    tasks: list[Task]
    acceptance: AcceptanceCriteria
    termination: TerminationPolicy

    @model_validator(mode="before")
    @classmethod
    def _fill_termination(cls, data: Any) -> Any:
        return _with_defaults(data, {"termination": {"done_when": []}})

    @field_validator("assumptions", "open_questions", mode="before")
    @classmethod
    def _coerce_lists(cls, value: Any) -> Any:
        return _as_str_list(value)
