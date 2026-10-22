"""Audit logging — append-only, queryable event log."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Well-known audit actions
ACTIONS = [
    "api_key_created",
    "api_key_revoked",
    "team_created",
    "member_added",
    "member_removed",
    "run_started",
    "run_completed",
    "settings_changed",
    "login",
    "logout",
]


@dataclass
class AuditEvent:
    """A single auditable event."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    actor: str = ""  # user_id or api_key identifier
    action: str = ""
    resource_type: str = ""
    resource_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    ip_address: str | None = None
    result: str = "success"  # "success" or "failure"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEvent:
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


class AuditLogger:
    """Append-only audit log writer with query support.

    Writes events as JSONL to a file for tamper-evident, line-by-line auditing.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def log_path(self) -> Path:
        return self._log_path

    def log(self, event: AuditEvent) -> None:
        """Persist an audit event to the JSONL log."""
        with open(self._log_path, "a") as f:
            f.write(json.dumps(event.to_dict(), default=str) + "\n")
        logger.debug("Audit event %s: %s on %s", event.event_id, event.action, event.resource_type)

    def query(
        self,
        actor: str | None = None,
        action: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Query audit events with optional filters.

        Returns up to *limit* matching events, newest first.
        """
        if not self._log_path.exists():
            return []

        results: list[AuditEvent] = []
        with open(self._log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    event = AuditEvent.from_dict(data)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue

                if actor is not None and event.actor != actor:
                    continue
                if action is not None and event.action != action:
                    continue
                if start is not None or end is not None:
                    event_ts = datetime.fromisoformat(event.timestamp)
                    if start is not None and event_ts < start:
                        continue
                    if end is not None and event_ts > end:
                        continue

                results.append(event)

        # Return newest first, capped at limit
        results.reverse()
        return results[:limit]


class AuditMiddleware(BaseHTTPMiddleware):  # type: ignore[misc, unused-ignore]
    """FastAPI middleware that auto-logs every API request as an audit event."""

    def __init__(self, app: Any, audit_logger: AuditLogger) -> None:
        super().__init__(app)
        self._audit_logger = audit_logger

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        actor = self._redact_actor(request)
        ip_address = request.client.host if request.client else None

        response = await call_next(request)

        result = "success" if response.status_code < 400 else "failure"
        event = AuditEvent(
            actor=actor,
            action=f"{request.method} {request.url.path}",
            resource_type="http_request",
            resource_id=self._sanitize_url(request),
            details={"status_code": response.status_code, "method": request.method},
            ip_address=ip_address,
            result=result,
        )
        self._audit_logger.log(event)

        return response

    @staticmethod
    def _redact_actor(request: Request) -> str:
        """Extract actor identity without logging raw secrets.

        Logs only a fingerprint (first 8 + last 4 chars) of the key,
        never the full bearer token or API key.
        """
        raw = request.headers.get("Authorization", "")
        if raw.startswith("Bearer "):
            key = raw.removeprefix("Bearer ").strip()
            if len(key) > 12:
                return f"key:{key[:8]}...{key[-4:]}"
            return "key:***"
        raw = request.headers.get("X-API-Key", "")
        if raw:
            if len(raw) > 12:
                return f"key:{raw[:8]}...{raw[-4:]}"
            return "key:***"
        return "anonymous"

    @staticmethod
    def _sanitize_url(request: Request) -> str:
        """Return the URL path without sensitive query parameters.

        Strips 'token', 'key', 'secret', and 'sig' params from the URL
        to prevent leaking credentials into audit logs.
        """
        path: str = str(request.url.path)
        params = dict(request.query_params)
        sensitive_keys = {"token", "key", "secret", "sig", "api_key"}
        sanitized = {k: v for k, v in params.items() if k.lower() not in sensitive_keys}
        if sanitized:
            qs = "&".join(f"{k}={v}" for k, v in sanitized.items())
            return f"{path}?{qs}"
        return path
