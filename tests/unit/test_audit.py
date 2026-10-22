"""Tests for SOC 2 audit logging module."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from delegatron.audit import ACTIONS, AuditEvent, AuditLogger, AuditMiddleware

# ---------------------------------------------------------------------------
# AuditEvent
# ---------------------------------------------------------------------------


class TestAuditEvent:
    def test_defaults(self) -> None:
        event = AuditEvent()
        assert event.actor == ""
        assert event.action == ""
        assert event.result == "success"
        assert event.ip_address is None
        assert isinstance(event.details, dict)
        assert len(event.event_id) == 32  # uuid4().hex

    def test_to_dict_roundtrip(self) -> None:
        event = AuditEvent(
            actor="user-1",
            action="login",
            resource_type="session",
            resource_id="sess-42",
            details={"browser": "firefox"},
            ip_address="10.0.0.1",
            result="success",
        )
        d = event.to_dict()
        assert d["actor"] == "user-1"
        assert d["action"] == "login"
        assert d["details"] == {"browser": "firefox"}

    def test_from_dict(self) -> None:
        data = {
            "event_id": "abc123",
            "actor": "bot",
            "action": "run_started",
            "result": "failure",
        }
        event = AuditEvent.from_dict(data)
        assert event.event_id == "abc123"
        assert event.actor == "bot"
        assert event.result == "failure"

    def test_from_dict_ignores_extra_keys(self) -> None:
        data = {"actor": "u1", "unknown_field": "ignored"}
        event = AuditEvent.from_dict(data)
        assert event.actor == "u1"
        assert not hasattr(event, "unknown_field")

    def test_from_dict_empty(self) -> None:
        event = AuditEvent.from_dict({})
        assert event.actor == ""


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class TestAuditLogger:
    def test_log_creates_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        event = AuditEvent(actor="u1", action="login")
        logger.log(event)

        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["actor"] == "u1"
        assert parsed["action"] == "login"

    def test_log_appends(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        logger.log(AuditEvent(actor="a"))
        logger.log(AuditEvent(actor="b"))

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_log_creates_parent_dirs(self, tmp_path: Path) -> None:
        log_file = tmp_path / "sub" / "dir" / "audit.jsonl"
        AuditLogger(log_file)
        assert log_file.parent.exists()

    def test_log_path_property(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        assert logger.log_path == log_file

    def test_query_empty_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        assert logger.query() == []

    def test_query_nonexistent_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "missing.jsonl"
        logger = AuditLogger(log_file)
        assert logger.query() == []

    def test_query_returns_all(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        for i in range(3):
            logger.log(AuditEvent(actor=f"u{i}", action="login"))

        results = logger.query()
        assert len(results) == 3

    def test_query_newest_first(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        logger.log(AuditEvent(actor="first"))
        logger.log(AuditEvent(actor="second"))

        results = logger.query()
        assert results[0].actor == "second"
        assert results[1].actor == "first"

    def test_query_filter_by_actor(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        logger.log(AuditEvent(actor="alice", action="login"))
        logger.log(AuditEvent(actor="bob", action="login"))

        results = logger.query(actor="alice")
        assert len(results) == 1
        assert results[0].actor == "alice"

    def test_query_filter_by_action(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        logger.log(AuditEvent(actor="u1", action="login"))
        logger.log(AuditEvent(actor="u1", action="logout"))

        results = logger.query(action="logout")
        assert len(results) == 1
        assert results[0].action == "logout"

    def test_query_filter_by_time_range(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)

        now = datetime.now(UTC)
        old = AuditEvent(actor="old", timestamp=(now - timedelta(hours=2)).isoformat())
        recent = AuditEvent(actor="recent", timestamp=(now - timedelta(minutes=5)).isoformat())
        logger.log(old)
        logger.log(recent)

        results = logger.query(start=now - timedelta(hours=1))
        assert len(results) == 1
        assert results[0].actor == "recent"

    def test_query_filter_end(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)

        now = datetime.now(UTC)
        old = AuditEvent(actor="old", timestamp=(now - timedelta(hours=2)).isoformat())
        recent = AuditEvent(actor="recent", timestamp=(now - timedelta(minutes=5)).isoformat())
        logger.log(old)
        logger.log(recent)

        results = logger.query(end=now - timedelta(hours=1))
        assert len(results) == 1
        assert results[0].actor == "old"

    def test_query_limit(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_file)
        for i in range(10):
            logger.log(AuditEvent(actor=f"u{i}"))

        results = logger.query(limit=3)
        assert len(results) == 3

    def test_query_skips_blank_lines(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        event = AuditEvent(actor="x")
        log_file.write_text(
            json.dumps(event.to_dict()) + "\n\n" + json.dumps(event.to_dict()) + "\n"
        )
        logger = AuditLogger(log_file)
        assert len(logger.query()) == 2


# ---------------------------------------------------------------------------
# AuditMiddleware
# ---------------------------------------------------------------------------


class TestAuditMiddleware:
    @pytest.mark.asyncio
    async def test_dispatch_logs_success(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        audit_logger = AuditLogger(log_file)

        app = MagicMock()
        middleware = AuditMiddleware(app, audit_logger=audit_logger)

        request = MagicMock()
        request.headers = {"X-API-Key": "dlg_abcdefghijklmnop1234"}
        request.query_params = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        request.method = "GET"
        request.url.path = "/api/v1/runs"
        request.url.__str__ = lambda self: "http://localhost/api/v1/runs"

        response = MagicMock()
        response.status_code = 200
        call_next = AsyncMock(return_value=response)

        result = await middleware.dispatch(request, call_next)

        assert result is response
        call_next.assert_awaited_once_with(request)
        events = audit_logger.query()
        assert len(events) == 1
        # Actor should be redacted (fingerprint, not raw key)
        assert events[0].actor.startswith("key:")
        assert "dlg_abcdefghijklmnop1234" not in events[0].actor
        assert events[0].result == "success"
        assert events[0].ip_address == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_dispatch_logs_failure(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        audit_logger = AuditLogger(log_file)

        app = MagicMock()
        middleware = AuditMiddleware(app, audit_logger=audit_logger)

        request = MagicMock()
        request.headers = {"Authorization": "Bearer dlg_a1b2c3d4e5f6g7h81234"}
        request.query_params = {}
        request.client = MagicMock()
        request.client.host = "10.0.0.1"
        request.method = "POST"
        request.url.path = "/api/v1/runs"
        request.url.__str__ = lambda self: "http://localhost/api/v1/runs"

        response = MagicMock()
        response.status_code = 403
        call_next = AsyncMock(return_value=response)

        await middleware.dispatch(request, call_next)

        events = audit_logger.query()
        assert events[0].result == "failure"
        # Actor should be redacted — never log raw bearer tokens
        assert events[0].actor.startswith("key:")
        assert "dlg_a1b2c3d4e5f6g7h81234" not in events[0].actor

    @pytest.mark.asyncio
    async def test_dispatch_anonymous_no_client(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        audit_logger = AuditLogger(log_file)

        app = MagicMock()
        middleware = AuditMiddleware(app, audit_logger=audit_logger)

        request = MagicMock()
        request.headers = {}
        request.query_params = {}
        request.client = None
        request.method = "GET"
        request.url.path = "/health"
        request.url.__str__ = lambda self: "http://localhost/health"

        response = MagicMock()
        response.status_code = 200
        call_next = AsyncMock(return_value=response)

        await middleware.dispatch(request, call_next)

        events = audit_logger.query()
        assert events[0].actor == "anonymous"
        assert events[0].ip_address is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"token": "TOKVAL", "page": "2"}, "/api/runs/r1/stream?page=2"),
            (
                {
                    "key": "KEYVAL",
                    "Secret": "SECVAL",
                    "sig": "SIGVAL",
                    "api_key": "APIVAL",
                    "page": "2",
                    "q": "y",
                },
                "/api/runs/r1/stream?page=2&q=y",
            ),
            ({"token": "TOKVAL", "SIG": "SIGVAL"}, "/api/runs/r1/stream"),
        ],
    )
    async def test_dispatch_strips_sensitive_query_params(
        self, tmp_path: Path, params: dict[str, str], expected: str
    ) -> None:
        audit_logger = AuditLogger(tmp_path / "audit.jsonl")
        middleware = AuditMiddleware(MagicMock(), audit_logger=audit_logger)

        request = MagicMock()
        request.headers = {}
        request.query_params = params
        request.client = None
        request.method = "GET"
        request.url.path = "/api/runs/r1/stream"

        response = MagicMock()
        response.status_code = 200
        await middleware.dispatch(request, AsyncMock(return_value=response))

        [event] = audit_logger.query()
        assert event.resource_id == expected
        for secret in ("TOKVAL", "KEYVAL", "SECVAL", "SIGVAL", "APIVAL"):
            assert secret not in json.dumps(event.to_dict())


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_actions_list() -> None:
    assert "login" in ACTIONS
    assert "logout" in ACTIONS
    assert "run_started" in ACTIONS
    assert len(ACTIONS) > 0
