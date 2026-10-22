"""Tests for delegatron.topology — self-organizing agent topology."""

from __future__ import annotations

import pytest

from delegatron.routing import ModelEndpoint, ModelRouter, RoutingStrategy
from delegatron.topology import AgentInstance, AgentRole, AgentStatus, TopologyManager

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_endpoint(
    provider: str = "openai",
    model: str = "gpt-4",
) -> ModelEndpoint:
    return ModelEndpoint(
        provider=provider,
        model=model,
        api_key="test-key",
        max_tokens=8192,
        cost_per_1k_input=0.03,
        cost_per_1k_output=0.06,
    )


@pytest.fixture
def router() -> ModelRouter:
    eps = [
        _make_endpoint("openai", "gpt-4"),
        _make_endpoint("anthropic", "claude-sonnet"),
        _make_endpoint("google", "gemini-pro"),
    ]
    return ModelRouter(eps, RoutingStrategy.ROUND_ROBIN)


@pytest.fixture
def roles() -> list[AgentRole]:
    return [
        AgentRole(
            name="architect",
            description="Plans tasks",
            required_capabilities=["planning"],
            min_instances=1,
            max_instances=3,
        ),
        AgentRole(
            name="implementer",
            description="Writes code",
            required_capabilities=["coding"],
            min_instances=1,
            max_instances=2,
        ),
    ]


@pytest.fixture
def topology(roles: list[AgentRole], router: ModelRouter) -> TopologyManager:
    return TopologyManager(roles, router)


# ------------------------------------------------------------------
# Initialization
# ------------------------------------------------------------------


class TestTopologyInit:
    def test_creates_min_instances(self, topology: TopologyManager) -> None:
        topo = topology.get_topology()
        assert len(topo["architect"]) == 1
        assert len(topo["implementer"]) == 1

    def test_instances_start_idle(self, topology: TopologyManager) -> None:
        topo = topology.get_topology()
        for role_instances in topo.values():
            for inst in role_instances:
                assert inst["status"] == "idle"


# ------------------------------------------------------------------
# get_agent
# ------------------------------------------------------------------


class TestGetAgent:
    def test_returns_idle_instance(self, topology: TopologyManager) -> None:
        agent = topology.get_agent("architect")
        assert isinstance(agent, AgentInstance)
        assert agent.status == AgentStatus.IDLE

    def test_unknown_role_raises(self, topology: TopologyManager) -> None:
        with pytest.raises(KeyError, match="Unknown role"):
            topology.get_agent("nonexistent")

    def test_prefers_higher_quality(self, topology: TopologyManager) -> None:
        # Scale up to have two architect instances
        topology.scale_up("architect")
        topo = topology.get_topology()
        ids = [inst["instance_id"] for inst in topo["architect"]]
        # Give second instance higher quality
        topology.report_quality(ids[0], 0.5)
        topology.report_quality(ids[1], 0.9)
        best = topology.get_agent("architect")
        assert best.instance_id == ids[1]


# ------------------------------------------------------------------
# scale_up / scale_down
# ------------------------------------------------------------------


class TestScaling:
    def test_scale_up_adds_instance(self, topology: TopologyManager) -> None:
        result = topology.scale_up("architect")
        assert result is not None
        topo = topology.get_topology()
        assert len(topo["architect"]) == 2

    def test_scale_up_respects_max(self, topology: TopologyManager) -> None:
        topology.scale_up("architect")
        topology.scale_up("architect")
        # max_instances is 3, so third scale_up should succeed
        assert len(topology.get_topology()["architect"]) == 3
        # fourth should fail
        result = topology.scale_up("architect")
        assert result is None
        assert len(topology.get_topology()["architect"]) == 3

    def test_scale_down_removes_instance(self, topology: TopologyManager) -> None:
        topology.scale_up("architect")
        assert len(topology.get_topology()["architect"]) == 2
        removed = topology.scale_down("architect")
        assert removed is True
        assert len(topology.get_topology()["architect"]) == 1

    def test_scale_down_respects_min(self, topology: TopologyManager) -> None:
        # Already at min_instances=1
        removed = topology.scale_down("architect")
        assert removed is False
        assert len(topology.get_topology()["architect"]) == 1

    def test_scale_down_removes_lowest_quality(self, topology: TopologyManager) -> None:
        topology.scale_up("architect")
        topo = topology.get_topology()
        ids = [inst["instance_id"] for inst in topo["architect"]]
        topology.report_quality(ids[0], 0.9)
        topology.report_quality(ids[1], 0.3)
        topology.scale_down("architect")
        remaining = topology.get_topology()["architect"]
        assert len(remaining) == 1
        assert remaining[0]["instance_id"] == ids[0]

    def test_scale_unknown_role_raises(self, topology: TopologyManager) -> None:
        with pytest.raises(KeyError):
            topology.scale_up("nonexistent")
        with pytest.raises(KeyError):
            topology.scale_down("nonexistent")


# ------------------------------------------------------------------
# report_quality
# ------------------------------------------------------------------


class TestReportQuality:
    def test_updates_quality_score(self, topology: TopologyManager) -> None:
        agent = topology.get_agent("architect")
        topology.report_quality(agent.instance_id, 0.8)
        assert agent.avg_quality_score == pytest.approx(0.8)
        topology.report_quality(agent.instance_id, 0.6)
        assert agent.avg_quality_score == pytest.approx(0.7)
        assert agent.tasks_completed == 2

    def test_unknown_instance_raises(self, topology: TopologyManager) -> None:
        with pytest.raises(KeyError, match="Unknown instance"):
            topology.report_quality("nonexistent-id", 0.5)


# ------------------------------------------------------------------
# rebalance
# ------------------------------------------------------------------


class TestRebalance:
    def test_scales_up_when_all_busy(self, topology: TopologyManager) -> None:
        agent = topology.get_agent("architect")
        agent.status = AgentStatus.BUSY
        actions = topology.rebalance()
        scale_ups = [a for a in actions if a.startswith("scale_up:architect")]
        assert len(scale_ups) == 1
        assert len(topology.get_topology()["architect"]) == 2

    def test_scales_down_excess_idle(self, topology: TopologyManager) -> None:
        topology.scale_up("architect")
        topology.scale_up("architect")
        # All 3 idle, min is 1 -> should scale down
        actions = topology.rebalance()
        scale_downs = [a for a in actions if a.startswith("scale_down:architect")]
        assert len(scale_downs) == 1

    def test_replaces_low_quality(self, topology: TopologyManager) -> None:
        agent = topology.get_agent("architect")
        # Report enough low-quality tasks to trigger replacement
        for _ in range(4):
            topology.report_quality(agent.instance_id, 0.2)
        old_id = agent.instance_id
        actions = topology.rebalance()
        replacements = [a for a in actions if a.startswith("replace:architect")]
        assert len(replacements) == 1
        assert old_id in replacements[0]

    def test_recovers_failed_instances(self, topology: TopologyManager) -> None:
        agent = topology.get_agent("architect")
        agent.status = AgentStatus.FAILED
        actions = topology.rebalance()
        recoveries = [a for a in actions if a.startswith("recover:")]
        # May also scale up since no non-failed idle exist
        assert any("recover:architect" in a for a in recoveries)

    def test_no_actions_when_balanced(self, topology: TopologyManager) -> None:
        # Default state: 1 idle instance per role, at min — should be balanced
        actions = topology.rebalance()
        assert actions == []


# ------------------------------------------------------------------
# get_topology
# ------------------------------------------------------------------


class TestGetTopology:
    def test_structure(self, topology: TopologyManager) -> None:
        topo = topology.get_topology()
        assert "architect" in topo
        assert "implementer" in topo
        for role_instances in topo.values():
            for inst in role_instances:
                assert "instance_id" in inst
                assert "endpoint" in inst
                assert "status" in inst
                assert "tasks_completed" in inst
                assert "avg_quality_score" in inst
