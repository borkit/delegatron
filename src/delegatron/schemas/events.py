"""Event envelope schema for pipeline observability."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """Token counts for a single API call."""

    input: int = 0
    output: int = 0


class EventPayload(BaseModel):
    """Payload for a pipeline event."""

    agent: str
    artifact_uri: str = ""
    tokens: TokenUsage = TokenUsage()
    latency_ms: float = 0.0


class PipelineEvent(BaseModel):
    """Envelope for all pipeline events.

    Stored in the event log for auditability and observability.
    """

    event_id: str
    event_type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str
    task_id: str = ""
    attempt: int = 1
    producer: str = "orchestrator"
    payload: EventPayload
