"""ArbitrationReport schema — director output."""

from __future__ import annotations

import logging
from typing import Any, Literal, get_args

from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

Decision = Literal["ACCEPT", "REVISE", "DEFER"]


class Resolution(BaseModel):
    """The director's binding decision on one implementer/reviewer conflict."""

    title: str
    decision: Decision
    reasoning: str = ""
    alternative: str = ""  # REVISE: the one concrete change to make instead
    # "architecture": no code or test change within the task can satisfy it;
    # the plan itself must change (the orchestrator asks the architect to re-plan)
    fix_target: Literal["code", "test", "architecture"] | None = None
    fix_file: str | None = None

    @field_validator("decision", mode="before")
    @classmethod
    def _coerce_decision(cls, v: Any) -> str:
        """Accept any letter case and stray whitespace ("revise", " Accept").

        Anything else becomes REVISE, which keeps the conflict open: an
        unreadable ruling must never dismiss a reviewer issue.
        """
        decision = v.strip().upper() if isinstance(v, str) else ""
        if decision in get_args(Decision):
            return decision
        logger.warning("Director decision %r treated as 'REVISE'", v)
        return "REVISE"

    @field_validator("fix_target", mode="before")
    @classmethod
    def _coerce_fix_target(cls, v: Any) -> str | None:
        """Normalise case; unknown values (e.g. "both") become None."""
        target = v.strip().lower() if isinstance(v, str) else None
        return target if target in ("code", "test", "architecture") else None


class ArbitrationReport(BaseModel):
    """Structured output from the director agent: one resolution per conflict."""

    resolutions: list[Resolution] = []
