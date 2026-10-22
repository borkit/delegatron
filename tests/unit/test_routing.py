"""Tests for delegatron.routing — model routing strategies."""

from __future__ import annotations

import pytest

from delegatron.routing import ModelEndpoint, ModelRouter, RoutingStrategy

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_endpoint(
    provider: str = "openai",
    model: str = "gpt-4",
    cost_in: float = 0.03,
    cost_out: float = 0.06,
    latency: float = 500.0,
    capabilities: list[str] | None = None,
    weight: float = 1.0,
) -> ModelEndpoint:
    return ModelEndpoint(
        provider=provider,
        model=model,
        api_key="test-key",
        max_tokens=8192,
        cost_per_1k_input=cost_in,
        cost_per_1k_output=cost_out,
        avg_latency_ms=latency,
        capabilities=capabilities or ["all"],
        weight=weight,
    )


@pytest.fixture
def cheap_endpoint() -> ModelEndpoint:
    return _make_endpoint(provider="google", model="gemini-flash", cost_in=0.001, cost_out=0.002)


@pytest.fixture
def fast_endpoint() -> ModelEndpoint:
    return _make_endpoint(provider="openai", model="gpt-4o-mini", latency=100.0)


@pytest.fixture
def expensive_endpoint() -> ModelEndpoint:
    return _make_endpoint(provider="anthropic", model="claude-opus", cost_in=0.10, cost_out=0.30)


@pytest.fixture
def endpoints(
    cheap_endpoint: ModelEndpoint,
    fast_endpoint: ModelEndpoint,
    expensive_endpoint: ModelEndpoint,
) -> list[ModelEndpoint]:
    return [cheap_endpoint, fast_endpoint, expensive_endpoint]


# ------------------------------------------------------------------
# Constructor
# ------------------------------------------------------------------


class TestModelRouterInit:
    def test_requires_at_least_one_endpoint(self) -> None:
        with pytest.raises(ValueError, match="At least one endpoint"):
            ModelRouter(endpoints=[], strategy=RoutingStrategy.FIXED)

    def test_creates_stats_for_all_endpoints(self, endpoints: list[ModelEndpoint]) -> None:
        router = ModelRouter(endpoints)
        stats = router.get_stats()
        assert len(stats) == 3


# ------------------------------------------------------------------
# FIXED strategy
# ------------------------------------------------------------------


class TestFixedStrategy:
    def test_always_returns_first(self, endpoints: list[ModelEndpoint]) -> None:
        router = ModelRouter(endpoints, RoutingStrategy.FIXED)
        for _ in range(5):
            assert router.select("architect") is endpoints[0]

    def test_skips_unavailable(self, endpoints: list[ModelEndpoint]) -> None:
        router = ModelRouter(endpoints, RoutingStrategy.FIXED)
        endpoints[0].is_available = False
        assert router.select("architect") is endpoints[1]


# ------------------------------------------------------------------
# ROUND_ROBIN strategy
# ------------------------------------------------------------------


class TestRoundRobinStrategy:
    def test_cycles_through_endpoints(self, endpoints: list[ModelEndpoint]) -> None:
        router = ModelRouter(endpoints, RoutingStrategy.ROUND_ROBIN)
        results = [router.select("implementer") for _ in range(6)]
        # Should cycle: 0, 1, 2, 0, 1, 2
        assert results[0] is endpoints[0]
        assert results[1] is endpoints[1]
        assert results[2] is endpoints[2]
        assert results[3] is endpoints[0]

    def test_skips_unavailable_in_rotation(self, endpoints: list[ModelEndpoint]) -> None:
        endpoints[1].is_available = False
        router = ModelRouter(endpoints, RoutingStrategy.ROUND_ROBIN)
        results = [router.select("reviewer") for _ in range(4)]
        assert endpoints[1] not in results


# ------------------------------------------------------------------
# COST_OPTIMIZED strategy
# ------------------------------------------------------------------


class TestCostOptimizedStrategy:
    def test_selects_cheapest(
        self, endpoints: list[ModelEndpoint], cheap_endpoint: ModelEndpoint
    ) -> None:
        router = ModelRouter(endpoints, RoutingStrategy.COST_OPTIMIZED)
        assert router.select("reviewer") is cheap_endpoint

    def test_falls_back_if_cheapest_unavailable(
        self, endpoints: list[ModelEndpoint], fast_endpoint: ModelEndpoint
    ) -> None:
        endpoints[0].is_available = False  # cheap_endpoint
        router = ModelRouter(endpoints, RoutingStrategy.COST_OPTIMIZED)
        result = router.select("reviewer")
        assert result is fast_endpoint

    def test_weight_affects_cost_ranking(self) -> None:
        ep1 = _make_endpoint(model="a", cost_in=0.10, cost_out=0.10, weight=1.0)
        ep2 = _make_endpoint(model="b", cost_in=0.15, cost_out=0.15, weight=3.0)
        router = ModelRouter([ep1, ep2], RoutingStrategy.COST_OPTIMIZED)
        # ep2 has higher raw cost but weight=3 makes effective cost lower
        assert router.select("implementer") is ep2


# ------------------------------------------------------------------
# LATENCY_OPTIMIZED strategy
# ------------------------------------------------------------------


class TestLatencyOptimizedStrategy:
    def test_selects_lowest_latency(
        self, endpoints: list[ModelEndpoint], fast_endpoint: ModelEndpoint
    ) -> None:
        router = ModelRouter(endpoints, RoutingStrategy.LATENCY_OPTIMIZED)
        assert router.select("implementer") is fast_endpoint


# ------------------------------------------------------------------
# CAPABILITY_BASED strategy
# ------------------------------------------------------------------


class TestCapabilityBasedStrategy:
    def test_prefers_matching_capabilities(self) -> None:
        generic = _make_endpoint(model="generic", capabilities=["all"])
        coder = _make_endpoint(
            model="coder", capabilities=["coding", "implementation", "debugging"], weight=1.0
        )
        router = ModelRouter([generic, coder], RoutingStrategy.CAPABILITY_BASED)
        assert router.select("implementer") is coder

    def test_falls_back_to_all_when_no_match(self) -> None:
        generic = _make_endpoint(model="generic", capabilities=["all"])
        niche = _make_endpoint(model="niche", capabilities=["translation"])
        router = ModelRouter([generic, niche], RoutingStrategy.CAPABILITY_BASED)
        # "architect" needs planning/decomposition/architecture — niche has none
        assert router.select("architect") is generic


# ------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------


class TestReporting:
    def test_report_latency_updates_average(self) -> None:
        ep = _make_endpoint(latency=0.0)
        router = ModelRouter([ep])
        router.report_latency(ep, 200.0)
        assert ep.avg_latency_ms == 200.0
        router.report_latency(ep, 400.0)
        # EMA: 0.7 * 200 + 0.3 * 400 = 260
        assert abs(ep.avg_latency_ms - 260.0) < 0.01

    def test_report_failure_marks_unavailable(self) -> None:
        ep = _make_endpoint()
        router = ModelRouter([ep])
        router.report_failure(ep)
        assert not ep.is_available
        stats = router.get_stats()
        key = f"{ep.provider}/{ep.model}"
        assert stats[key]["failures"] == 1

    def test_report_success_marks_available(self) -> None:
        ep = _make_endpoint()
        router = ModelRouter([ep])
        router.report_failure(ep)
        assert not ep.is_available
        router.report_success(ep)
        assert ep.is_available

    def test_no_available_endpoints_raises(self) -> None:
        ep = _make_endpoint()
        router = ModelRouter([ep])
        ep.is_available = False
        with pytest.raises(RuntimeError, match="No available endpoints"):
            router.select("architect")

    def test_get_stats_structure(self, endpoints: list[ModelEndpoint]) -> None:
        router = ModelRouter(endpoints)
        stats = router.get_stats()
        for _key, data in stats.items():
            assert "requests" in data
            assert "failures" in data
            assert "avg_latency_ms" in data
            assert "is_available" in data
