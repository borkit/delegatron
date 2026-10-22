"""Custom model routing for Delegatron.

Supports multiple routing strategies to distribute requests across model
endpoints based on cost, latency, capability matching, or simple rotation.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# Capabilities that map to agent roles by default
DEFAULT_ROLE_CAPABILITIES: dict[str, list[str]] = {
    "architect": ["planning", "decomposition", "architecture"],
    "implementer": ["coding", "implementation", "debugging"],
    "reviewer": ["review", "analysis", "security"],
}

# Task complexity weights — higher means prefer stronger models
COMPLEXITY_WEIGHTS: dict[str, float] = {
    "low": 0.5,
    "medium": 1.0,
    "high": 2.0,
}


class RoutingStrategy(Enum):
    """Available strategies for selecting a model endpoint."""

    FIXED = "fixed"
    ROUND_ROBIN = "round_robin"
    COST_OPTIMIZED = "cost_optimized"
    LATENCY_OPTIMIZED = "latency_optimized"
    CAPABILITY_BASED = "capability_based"


@dataclass
class ModelEndpoint:
    """A single model endpoint with metadata for routing decisions."""

    provider: str
    model: str
    api_key: str
    max_tokens: int
    cost_per_1k_input: float
    cost_per_1k_output: float
    avg_latency_ms: float = 0.0
    capabilities: list[str] = field(default_factory=lambda: ["all"])
    is_available: bool = True
    weight: float = 1.0


@dataclass
class _EndpointStats:
    """Internal tracking for an endpoint."""

    requests: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    last_failure_time: float = 0.0


class ModelRouter:
    """Route model requests across endpoints using a pluggable strategy."""

    def __init__(
        self,
        endpoints: list[ModelEndpoint],
        strategy: RoutingStrategy = RoutingStrategy.FIXED,
    ) -> None:
        if not endpoints:
            raise ValueError("At least one endpoint is required")
        self._endpoints = list(endpoints)
        self._strategy = strategy
        self._stats: dict[str, _EndpointStats] = {
            self._endpoint_key(ep): _EndpointStats() for ep in endpoints
        }
        self._rr_index = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(self, role: str, task_complexity: str = "medium") -> ModelEndpoint:
        """Pick the best endpoint for a role and task complexity.

        Raises:
            RuntimeError: If no available endpoints match the request.
        """
        with self._lock:
            available = [ep for ep in self._endpoints if ep.is_available]
            if not available:
                raise RuntimeError("No available endpoints")

            if self._strategy == RoutingStrategy.FIXED:
                return self._select_fixed(available)
            elif self._strategy == RoutingStrategy.ROUND_ROBIN:
                return self._select_round_robin(available)
            elif self._strategy == RoutingStrategy.COST_OPTIMIZED:
                return self._select_cost_optimized(available, role, task_complexity)
            elif self._strategy == RoutingStrategy.LATENCY_OPTIMIZED:
                return self._select_latency_optimized(available, role)
            elif self._strategy == RoutingStrategy.CAPABILITY_BASED:
                return self._select_capability_based(available, role, task_complexity)
            else:  # pragma: no cover
                return available[0]

    def report_latency(self, endpoint: ModelEndpoint, latency_ms: float) -> None:
        """Update running average latency for an endpoint."""
        with self._lock:
            key = self._endpoint_key(endpoint)
            stats = self._stats.get(key)
            if stats is None:
                return
            stats.requests += 1
            # Exponential moving average (alpha = 0.3)
            if endpoint.avg_latency_ms == 0.0:
                endpoint.avg_latency_ms = latency_ms
            else:
                endpoint.avg_latency_ms = 0.7 * endpoint.avg_latency_ms + 0.3 * latency_ms
            stats.total_latency_ms += latency_ms

    def report_failure(self, endpoint: ModelEndpoint) -> None:
        """Mark an endpoint as temporarily unavailable."""
        with self._lock:
            key = self._endpoint_key(endpoint)
            stats = self._stats.get(key)
            if stats is not None:
                stats.failures += 1
                stats.last_failure_time = time.monotonic()
            endpoint.is_available = False
        logger.warning("Endpoint marked unavailable: %s/%s", endpoint.provider, endpoint.model)

    def report_success(self, endpoint: ModelEndpoint) -> None:
        """Mark an endpoint as available (recover from failure)."""
        with self._lock:
            endpoint.is_available = True

    def get_stats(self) -> dict[str, dict[str, float | int]]:
        """Return usage statistics per endpoint."""
        result: dict[str, dict[str, float | int]] = {}
        for ep in self._endpoints:
            key = self._endpoint_key(ep)
            stats = self._stats.get(key, _EndpointStats())
            result[key] = {
                "requests": stats.requests,
                "failures": stats.failures,
                "avg_latency_ms": round(ep.avg_latency_ms, 2),
                "is_available": int(ep.is_available),
            }
        return result

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _select_fixed(self, available: list[ModelEndpoint]) -> ModelEndpoint:
        """Return the first available endpoint (stable selection)."""
        return available[0]

    def _select_round_robin(self, available: list[ModelEndpoint]) -> ModelEndpoint:
        """Rotate through available endpoints."""
        idx = self._rr_index % len(available)
        self._rr_index += 1
        return available[idx]

    def _select_cost_optimized(
        self,
        available: list[ModelEndpoint],
        role: str,
        task_complexity: str,
    ) -> ModelEndpoint:
        """Prefer cheapest endpoint that has the required capability."""
        capable = self._filter_capable(available, role)
        if not capable:
            capable = available
        # Sort by blended cost (input + output equally weighted)
        return min(
            capable,
            key=lambda ep: (ep.cost_per_1k_input + ep.cost_per_1k_output) / ep.weight,
        )

    def _select_latency_optimized(
        self,
        available: list[ModelEndpoint],
        role: str,
    ) -> ModelEndpoint:
        """Prefer lowest latency endpoint."""
        capable = self._filter_capable(available, role)
        if not capable:
            capable = available
        return min(capable, key=lambda ep: ep.avg_latency_ms)

    def _select_capability_based(
        self,
        available: list[ModelEndpoint],
        role: str,
        task_complexity: str,
    ) -> ModelEndpoint:
        """Score endpoints by capability match and complexity fit."""
        required = set(DEFAULT_ROLE_CAPABILITIES.get(role, []))
        complexity_w = COMPLEXITY_WEIGHTS.get(task_complexity, 1.0)

        def score(ep: ModelEndpoint) -> float:
            # Capability match score — specific matches beat "all"
            if not required:
                cap_score = 1.0
            elif "all" in ep.capabilities:
                cap_score = 0.9
            else:
                ep_caps = set(ep.capabilities)
                cap_score = len(required & ep_caps) / len(required)

            # Higher weight and max_tokens are better for complex tasks
            weight_score = ep.weight * complexity_w
            token_score = min(ep.max_tokens / 8192.0, 2.0)

            return cap_score * 3.0 + weight_score + token_score

        return max(available, key=score)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint_key(ep: ModelEndpoint) -> str:
        return f"{ep.provider}/{ep.model}"

    @staticmethod
    def _filter_capable(endpoints: list[ModelEndpoint], role: str) -> list[ModelEndpoint]:
        """Filter endpoints that have at least one capability for the role."""
        required = set(DEFAULT_ROLE_CAPABILITIES.get(role, []))
        if not required:
            return endpoints
        result = []
        for ep in endpoints:
            if "all" in ep.capabilities or set(ep.capabilities) & required:
                result.append(ep)
        return result
