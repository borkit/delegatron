"""Zero-cloud verification — report whether data left the local network.

After a pipeline run completes, produce an auditable report of every
network interaction the orchestrator recorded — LLM calls, sandbox
execution, artifact storage and other egress such as an E2B upload or a
``git push`` — and whether each stayed on the local network.

The report is an audit, not an enforcement: nothing is blocked.  Its
fingerprint is an integrity checksum over the whole report, not a signature.

Usage:
    guard = NetworkGuard.for_config(run_id, config)
    guard.record_llm_call(role="architect", provider="openai-compatible",
                          base_url="http://localhost:11434/v1", model="qwen3-coder:30b")
    guard.record_sandbox_call(sandbox_type="docker")
    guard.record_external_call("git_push", "github.com")
    report = guard.finalize()
    # report.cloud_free == False (the push left the network)
    # report.to_dict() → JSON-serializable compliance report
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from delegatron.utils.provider import is_local_host as provider_is_local_host

logger = logging.getLogger(__name__)

# Providers that always hit cloud APIs
_CLOUD_PROVIDERS = frozenset({"anthropic", "openai", "google", "litellm"})

# Sandbox types that execute on this machine; any other type (e2b, unknown) is cloud.
_LOCAL_SANDBOXES = frozenset({"docker", "local", "none"})

# External-call kinds that are cloud services whatever host they report.
_CLOUD_SERVICES = frozenset({"e2b"})

# Comma-separated extra host names that count as local (e.g. a LAN model server's FQDN).
LOCAL_HOSTS_ENV = "DELEGATRON_LOCAL_HOSTS"


def is_local_host(host: str, local_hosts: Collection[str] = ()) -> bool:
    """True when *host* is on this machine or the local network.

    The same rule as the model endpoints' (``utils.provider.is_local_host``:
    loopback, private, link-local and CGNAT addresses; ``localhost``, the
    LAN-only suffixes and single-label names), plus the unspecified address
    and the names listed in *local_hosts*.  No DNS lookups are made.
    """
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return False
    if provider_is_local_host(name):
        return True
    try:
        ip = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return name in {h.strip().rstrip(".").lower() for h in local_hosts}
    return ip.is_unspecified


def _host_of(url: str) -> str:
    """Host name of *url* (scheme optional), or ``""``."""
    try:
        return urlparse(url if "://" in url else f"//{url}").hostname or ""
    except ValueError:
        return ""


def _is_local_url(url: str, local_hosts: Collection[str] = ()) -> bool:
    """Check if a URL points to a local/private network address."""
    return is_local_host(_host_of(url), local_hosts) if url else False


def _env_local_hosts() -> frozenset[str]:
    raw = os.environ.get(LOCAL_HOSTS_ENV, "")
    return frozenset(h.strip() for h in raw.split(",") if h.strip())


def report_fingerprint(report: Mapping[str, Any]) -> str:
    """SHA-256 of a report dict (as written by ``to_dict``) minus its ``fingerprint``.

    Recompute it from a stored ``zero_cloud_report.json`` to detect edits.
    """
    content = {k: v for k, v in report.items() if k != "fingerprint"}
    canonical = json.dumps(content, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class NetworkEvent:
    """A single recorded network interaction."""

    timestamp: str
    category: str  # "llm_call", "sandbox", "artifact_store", or an external-call kind
    role: str
    provider: str
    host: str
    is_local: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ZeroCloudReport:
    """Report proving (or disproving) zero-cloud operation for a run."""

    run_id: str
    cloud_free: bool
    timestamp: str
    events: list[NetworkEvent]
    violations: list[dict[str, Any]]
    summary: dict[str, Any]
    fingerprint: str = ""  # report_fingerprint(to_dict()), set by NetworkGuard.finalize

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "cloud_free": self.cloud_free,
            "timestamp": self.timestamp,
            "total_events": len(self.events),
            "violations": self.violations,
            "summary": self.summary,
            "events": [
                {
                    "timestamp": e.timestamp,
                    "category": e.category,
                    "role": e.role,
                    "provider": e.provider,
                    "host": e.host,
                    "is_local": e.is_local,
                    "details": e.details,
                }
                for e in self.events
            ],
            "fingerprint": self.fingerprint,
        }


@dataclass
class NetworkGuard:
    """Records network interactions as the pipeline makes them.

    At finalization, produces a ZeroCloudReport that shows whether data
    stayed local. This report can be exported for compliance audits.
    """

    run_id: str = ""
    local_hosts: frozenset[str] = frozenset()
    _events: list[NetworkEvent] = field(default_factory=list)

    @classmethod
    def for_config(cls, run_id: str, config: Any) -> NetworkGuard:
        """Create the guard for a pipeline run.

        Locality is decided from recorded events, not from *config*; extra
        local host names come from ``DELEGATRON_LOCAL_HOSTS`` and the
        config's ``pipeline.local_hosts``.
        """
        pipeline = getattr(config, "pipeline", None)
        configured = getattr(pipeline, "local_hosts", ()) or ()
        hosts = _env_local_hosts() | frozenset(h.strip() for h in configured if h.strip())
        return cls(run_id=run_id, local_hosts=hosts)

    def _record(
        self,
        category: str,
        role: str,
        provider: str,
        host: str,
        is_local: bool,
        details: dict[str, Any],
    ) -> None:
        self._events.append(
            NetworkEvent(
                timestamp=datetime.now(UTC).isoformat(),
                category=category,
                role=role,
                provider=provider,
                host=host,
                is_local=is_local,
                details=details,
            )
        )

    def record_llm_call(
        self,
        role: str,
        provider: str,
        base_url: str = "",
        model: str = "",
    ) -> None:
        """Record an LLM provider call."""
        if provider == "mock":
            host = "mock"
            is_local = True
        elif provider in _CLOUD_PROVIDERS:
            host = f"{provider}-cloud-api"
            is_local = False
        else:
            host = _host_of(base_url) or "unknown"
            is_local = _is_local_url(base_url, self.local_hosts)

        self._record(
            "llm_call", role, provider, host, is_local, {"model": model, "base_url": base_url}
        )

    def record_sandbox_call(self, sandbox_type: str, host: str = "") -> None:
        """Record a sandbox execution; only docker/local/none sandboxes are local."""
        is_local = sandbox_type in _LOCAL_SANDBOXES
        host = host or ("localhost" if is_local else f"{sandbox_type}-cloud")
        self._record(
            "sandbox", "sandbox", sandbox_type, host, is_local, {"sandbox_type": sandbox_type}
        )

    def record_external_call(self, kind: str, host: str) -> None:
        """Record other egress, e.g. ``("e2b", host)`` or ``("git_push", remote_host)``.

        Locality follows *host*, except that cloud services such as E2B are
        never local and an empty host is never local.
        """
        is_local = kind not in _CLOUD_SERVICES and is_local_host(host, self.local_hosts)
        self._record(kind, "orchestrator", kind, host or "unknown", is_local, {})

    def record_artifact_store(
        self,
        store_type: str = "local_fs",
        path: str = "",
    ) -> None:
        """Record where artifacts are stored."""
        is_local = store_type in ("local_fs", "sqlite")
        self._record(
            "artifact_store",
            "storage",
            store_type,
            "localhost" if is_local else "cloud",
            is_local,
            {"store_type": store_type, "path": path},
        )

    def finalize(self) -> ZeroCloudReport:
        """Produce the final zero-cloud verification report."""
        events = list(self._events)
        violations = [
            {
                "timestamp": event.timestamp,
                "category": event.category,
                "role": event.role,
                "provider": event.provider,
                "host": event.host,
                "reason": f"Non-local {event.category}: {event.provider} → {event.host}",
            }
            for event in events
            if not event.is_local
        ]
        cloud_free = not violations

        by_category: dict[str, int] = {}
        by_provider: dict[str, int] = {}
        for event in events:
            by_category[event.category] = by_category.get(event.category, 0) + 1
            by_provider[event.provider] = by_provider.get(event.provider, 0) + 1
        local_count = sum(1 for event in events if event.is_local)

        report = ZeroCloudReport(
            run_id=self.run_id,
            cloud_free=cloud_free,
            timestamp=datetime.now(UTC).isoformat(),
            events=events,
            violations=violations,
            summary={
                "total_events": len(events),
                "local_events": local_count,
                "cloud_events": len(events) - local_count,
                "by_category": by_category,
                "by_provider": by_provider,
                "enforcement_mode": "audit",
            },
        )
        report.fingerprint = report_fingerprint(report.to_dict())

        if cloud_free:
            logger.info(
                "Zero-cloud VERIFIED for run %s: %d events, all local",
                self.run_id,
                len(events),
            )
        else:
            logger.warning(
                "Zero-cloud FAILED for run %s: %d violations out of %d events",
                self.run_id,
                len(violations),
                len(events),
            )

        return report
