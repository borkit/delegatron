"""ReviewReport schema — Gemini reviewer output."""

from __future__ import annotations

import logging
from typing import Any, Literal, get_args

from pydantic import BaseModel, field_validator, model_validator

logger = logging.getLogger(__name__)

Severity = Literal["blocker", "major", "minor", "nit"]
FixTarget = Literal["code", "test", "architecture"]

# Severity labels models use instead of the four the pipeline gates on.
_SEVERITY_SYNONYMS: dict[str, Severity] = {
    "critical": "blocker",
    "error": "blocker",
    "fatal": "blocker",
    "high": "major",
    "important": "major",
    "serious": "major",
    "medium": "minor",
    "moderate": "minor",
    "warning": "minor",
    "low": "nit",
    "info": "nit",
    "suggestion": "nit",
    "style": "nit",
    "trivial": "nit",
}
# Unrecognised labels fail closed: "major" blocks approval, so a typo never widens it.
_FALLBACK_SEVERITY: Severity = "major"

# Text fields a model may leave out; they become "" yet stay required in the JSON schema.
_FILLED_TEXT_FIELDS = ("details", "recommended_fix")


def _as_text(value: Any) -> Any:
    """``None`` becomes ``""`` and a list of strings becomes lines; anything else is kept."""
    if value is None:
        return ""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value)
    return value


class ReviewIssue(BaseModel):
    """A single issue found during review.

    ``fix_target`` says which side must change (the code, the test, or the
    plan itself) and ``fix_file`` names the one file to change.  Both are
    optional hints: values a model improvises are dropped, not rejected.

    Small slips in model output are repaired rather than rejected: severity
    synonyms and letter case are normalised, and missing or ``null`` text
    fields become empty strings.
    """

    severity: Severity
    title: str
    details: str
    recommended_fix: str
    evidence: str = ""
    fix_target: FixTarget | None = None
    fix_file: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _fill_missing_fields(cls, data: Any) -> Any:
        """Supply the keys a model left out (severity is then coerced as ``None``)."""
        if not isinstance(data, dict):
            return data
        missing: dict[str, Any] = {key: "" for key in _FILLED_TEXT_FIELDS if key not in data}
        if "severity" not in data:
            missing["severity"] = None
        return {**data, **missing} if missing else data

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: Any) -> str:
        """Map case variants and synonyms onto the four severities; unknown becomes major."""
        label = value.strip().lower() if isinstance(value, str) else ""
        if label in get_args(Severity):
            return label
        severity = _SEVERITY_SYNONYMS.get(label, _FALLBACK_SEVERITY)
        logger.warning("Review issue severity %r treated as %r", value, severity)
        return severity

    @field_validator("details", "recommended_fix", "evidence", mode="before")
    @classmethod
    def _coerce_text(cls, value: Any) -> Any:
        return _as_text(value)

    @field_validator("fix_target", mode="before")
    @classmethod
    def _coerce_fix_target(cls, value: Any) -> str | None:
        """Normalise case and whitespace; unknown targets become ``None``."""
        target = value.strip().lower() if isinstance(value, str) else None
        return target if target in get_args(FixTarget) else None

    @field_validator("fix_file", mode="before")
    @classmethod
    def _coerce_fix_file(cls, value: Any) -> str | None:
        """Strip whitespace; blank or non-string values become ``None``."""
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None


class ReviewReport(BaseModel):
    """Structured output from the Gemini reviewer agent.

    Contains a summary, acceptance verdict, and categorized issues.
    """

    summary: str
    acceptance_met: bool
    issues: list[ReviewIssue] = []

    @field_validator("issues", mode="before")
    @classmethod
    def _coerce_issues(cls, value: Any) -> Any:
        """``"issues": null`` means no issues."""
        return [] if value is None else value
