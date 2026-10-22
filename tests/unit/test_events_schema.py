"""Tests for the event envelope schema models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from delegatron.schemas.events import EventPayload, PipelineEvent, TokenUsage


class TestTokenUsage:
    """Tests for the TokenUsage model."""

    def test_defaults(self):
        t = TokenUsage()
        assert t.input == 0
        assert t.output == 0

    def test_custom_values(self):
        t = TokenUsage(input=100, output=50)
        assert t.input == 100
        assert t.output == 50

    def test_serialization(self):
        t = TokenUsage(input=10, output=20)
        d = t.model_dump()
        assert d == {"input": 10, "output": 20}

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            TokenUsage(input="not_a_number")


class TestEventPayload:
    """Tests for the EventPayload model."""

    def test_minimal_payload(self):
        p = EventPayload(agent="architect")
        assert p.agent == "architect"
        assert p.artifact_uri == ""
        assert p.tokens.input == 0
        assert p.tokens.output == 0
        assert p.latency_ms == 0.0

    def test_full_payload(self):
        p = EventPayload(
            agent="implementer",
            artifact_uri="s3://bucket/artifact.json",
            tokens=TokenUsage(input=500, output=300),
            latency_ms=1234.5,
        )
        assert p.agent == "implementer"
        assert p.artifact_uri == "s3://bucket/artifact.json"
        assert p.tokens.input == 500
        assert p.tokens.output == 300
        assert p.latency_ms == 1234.5

    def test_tokens_default_is_token_usage_instance(self):
        p = EventPayload(agent="reviewer")
        assert isinstance(p.tokens, TokenUsage)

    def test_agent_required(self):
        with pytest.raises(ValidationError):
            EventPayload()

    def test_serialization(self):
        p = EventPayload(agent="architect")
        d = p.model_dump()
        assert d["agent"] == "architect"
        assert "tokens" in d
        assert d["tokens"]["input"] == 0


class TestPipelineEvent:
    """Tests for the PipelineEvent envelope model."""

    def _make_event(self, **overrides):
        defaults = {
            "event_id": "evt-001",
            "event_type": "plan_complete",
            "run_id": "run-abc",
            "payload": EventPayload(agent="architect"),
        }
        defaults.update(overrides)
        return PipelineEvent(**defaults)

    def test_minimal_event(self):
        e = self._make_event()
        assert e.event_id == "evt-001"
        assert e.event_type == "plan_complete"
        assert e.run_id == "run-abc"
        assert e.task_id == ""
        assert e.attempt == 1
        assert e.producer == "orchestrator"
        assert isinstance(e.occurred_at, datetime)

    def test_custom_fields(self):
        e = self._make_event(
            task_id="task-1",
            attempt=3,
            producer="reviewer",
        )
        assert e.task_id == "task-1"
        assert e.attempt == 3
        assert e.producer == "reviewer"

    def test_occurred_at_auto_populated(self):
        before = datetime.now(UTC)
        e = self._make_event()
        after = datetime.now(UTC)
        assert e.occurred_at.tzinfo is not None
        assert before <= e.occurred_at <= after

    def test_occurred_at_custom(self):
        custom_dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        e = self._make_event(occurred_at=custom_dt)
        assert e.occurred_at == custom_dt

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            PipelineEvent(event_id="evt-001")

    def test_missing_event_id(self):
        with pytest.raises(ValidationError):
            PipelineEvent(
                event_type="plan_complete",
                run_id="run-abc",
                payload=EventPayload(agent="architect"),
            )

    def test_missing_payload(self):
        with pytest.raises(ValidationError):
            PipelineEvent(
                event_id="evt-001",
                event_type="plan_complete",
                run_id="run-abc",
            )

    def test_serialization_roundtrip(self):
        e = self._make_event(task_id="task-2", attempt=2)
        d = e.model_dump()
        e2 = PipelineEvent(**d)
        assert e2.event_id == e.event_id
        assert e2.task_id == e.task_id
        assert e2.attempt == e.attempt
        assert e2.payload.agent == e.payload.agent

    def test_payload_nested_in_dump(self):
        e = self._make_event()
        d = e.model_dump()
        assert isinstance(d["payload"], dict)
        assert d["payload"]["agent"] == "architect"
