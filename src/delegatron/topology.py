"""Self-organizing agent topology for Delegatron.

Manages dynamic pools of agent instances per role, with auto-scaling
based on load, quality, and availability.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum

from delegatron.routing import ModelEndpoint, ModelRouter

logger = logging.getLogger(__name__)

# Quality threshold below which an instance is considered low-performing
LOW_QUALITY_THRESHOLD = 0.4

# Minimum number of completed tasks before quality-based replacement
MIN_TASKS_FOR_QUALITY_EVAL = 3


class AgentStatus(Enum):
    """Runtime status of an agent instance."""

    IDLE = "idle"
    BUSY = "busy"
    FAILED = "failed"


@dataclass
class AgentRole:
    """Definition of an agent role in the topology."""

    name: str
    description: str
    required_capabilities: list[str]
    min_instances: int = 1
    max_instances: int = 1


@dataclass
class AgentInstance:
    """A running agent instance bound to a role and endpoint."""

    instance_id: str
    role: AgentRole
    endpoint: ModelEndpoint
    status: AgentStatus = AgentStatus.IDLE
    tasks_completed: int = 0
    avg_quality_score: float = 1.0

    # Internal tracking for quality running average
    _total_quality: float = field(default=0.0, repr=False)


class TopologyManager:
    """Manage a self-organizing pool of agent instances across roles."""

    def __init__(
        self,
        roles: list[AgentRole],
        router: ModelRouter,
    ) -> None:
        self._roles: dict[str, AgentRole] = {r.name: r for r in roles}
        self._router = router
        self._instances: dict[str, AgentInstance] = {}
        # Index: role_name -> list of instance_ids
        self._role_instances: dict[str, list[str]] = {r.name: [] for r in roles}

        # Bootstrap minimum instances for each role
        for role in roles:
            for _ in range(role.min_instances):
                self._create_instance(role)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_agent(self, role_name: str) -> AgentInstance:
        """Get the best available agent instance for a role.

        Prefers idle instances, sorted by quality score (highest first).
        If all are busy, returns the highest-quality instance.

        Raises:
            KeyError: If the role name is not registered.
            RuntimeError: If no instances exist for the role.
        """
        if role_name not in self._roles:
            raise KeyError(f"Unknown role: {role_name!r}")

        instance_ids = self._role_instances[role_name]
        if not instance_ids:
            raise RuntimeError(f"No instances available for role {role_name!r}")

        instances = [self._instances[iid] for iid in instance_ids]

        # Prefer idle, then sort by quality descending
        idle = [i for i in instances if i.status == AgentStatus.IDLE]
        if idle:
            return max(idle, key=lambda i: i.avg_quality_score)

        # No idle instances — return highest quality regardless of status
        non_failed = [i for i in instances if i.status != AgentStatus.FAILED]
        if non_failed:
            return max(non_failed, key=lambda i: i.avg_quality_score)

        return max(instances, key=lambda i: i.avg_quality_score)

    def scale_up(self, role_name: str) -> AgentInstance | None:
        """Add an instance for a role if under max_instances.

        Returns:
            The new AgentInstance, or None if already at max capacity.

        Raises:
            KeyError: If the role name is not registered.
        """
        if role_name not in self._roles:
            raise KeyError(f"Unknown role: {role_name!r}")

        role = self._roles[role_name]
        current_count = len(self._role_instances[role_name])

        if current_count >= role.max_instances:
            logger.debug("Cannot scale up %r: already at max (%d)", role_name, role.max_instances)
            return None

        instance = self._create_instance(role)
        logger.info("Scaled up %r: now %d instances", role_name, current_count + 1)
        return instance

    def scale_down(self, role_name: str) -> bool:
        """Remove an idle instance for a role if above min_instances.

        Removes the lowest-quality idle instance.

        Returns:
            True if an instance was removed, False otherwise.

        Raises:
            KeyError: If the role name is not registered.
        """
        if role_name not in self._roles:
            raise KeyError(f"Unknown role: {role_name!r}")

        role = self._roles[role_name]
        current_count = len(self._role_instances[role_name])

        if current_count <= role.min_instances:
            logger.debug("Cannot scale down %r: already at min (%d)", role_name, role.min_instances)
            return False

        # Find idle instances, pick the lowest quality
        idle = [
            self._instances[iid]
            for iid in self._role_instances[role_name]
            if self._instances[iid].status == AgentStatus.IDLE
        ]
        if not idle:
            return False

        victim = min(idle, key=lambda i: i.avg_quality_score)
        self._remove_instance(victim.instance_id)
        logger.info("Scaled down %r: removed %s", role_name, victim.instance_id)
        return True

    def report_quality(self, instance_id: str, score: float) -> None:
        """Record a quality score for a completed task.

        Args:
            instance_id: The agent instance ID.
            score: Quality score between 0.0 and 1.0.

        Raises:
            KeyError: If the instance ID is not found.
        """
        if instance_id not in self._instances:
            raise KeyError(f"Unknown instance: {instance_id!r}")

        instance = self._instances[instance_id]
        instance.tasks_completed += 1
        instance._total_quality += score
        instance.avg_quality_score = instance._total_quality / instance.tasks_completed

    def rebalance(self) -> list[str]:
        """Auto-scale based on load and quality. Returns list of actions taken."""
        actions: list[str] = []

        for role_name, role in self._roles.items():
            instance_ids = self._role_instances[role_name]
            instances = [self._instances[iid] for iid in instance_ids]

            # --- Scale up if all instances are busy ---
            non_failed = [i for i in instances if i.status != AgentStatus.FAILED]
            idle = [i for i in non_failed if i.status == AgentStatus.IDLE]

            if non_failed and not idle:
                result = self.scale_up(role_name)
                if result is not None:
                    actions.append(f"scale_up:{role_name}:{result.instance_id}")

            # --- Scale down excess idle instances ---
            current_count = len(instance_ids)
            if current_count > role.min_instances and len(idle) > 1 and self.scale_down(role_name):
                actions.append(f"scale_down:{role_name}")

            # --- Replace low-quality instances ---
            for inst in list(instances):
                if (
                    inst.tasks_completed >= MIN_TASKS_FOR_QUALITY_EVAL
                    and inst.avg_quality_score < LOW_QUALITY_THRESHOLD
                    and len(self._role_instances[role_name]) >= role.min_instances
                ):
                    # Remove old instance first to stay within max_instances
                    self._remove_instance(inst.instance_id)
                    replacement = self._create_instance(role)
                    actions.append(
                        f"replace:{role_name}:{inst.instance_id}->{replacement.instance_id}"
                    )

            # --- Recover failed instances ---
            failed = [i for i in instances if i.status == AgentStatus.FAILED]
            for inst in failed:
                if inst.instance_id in self._instances:
                    # Try a fresh endpoint
                    try:
                        new_ep = self._router.select(role_name)
                        inst.endpoint = new_ep
                        inst.status = AgentStatus.IDLE
                        actions.append(f"recover:{role_name}:{inst.instance_id}")
                    except RuntimeError:
                        pass

        return actions

    def get_topology(self) -> dict[str, list[dict[str, str | int | float]]]:
        """Return the current topology state as a serializable dict."""
        result: dict[str, list[dict[str, str | int | float]]] = {}
        for role_name, instance_ids in self._role_instances.items():
            role_instances: list[dict[str, str | int | float]] = []
            for iid in instance_ids:
                inst = self._instances[iid]
                role_instances.append(
                    {
                        "instance_id": inst.instance_id,
                        "endpoint": f"{inst.endpoint.provider}/{inst.endpoint.model}",
                        "status": inst.status.value,
                        "tasks_completed": inst.tasks_completed,
                        "avg_quality_score": round(inst.avg_quality_score, 3),
                    }
                )
            result[role_name] = role_instances
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_instance(self, role: AgentRole) -> AgentInstance:
        """Create a new agent instance and register it."""
        endpoint = self._router.select(role.name)
        instance_id = f"{role.name}-{uuid.uuid4().hex[:8]}"
        instance = AgentInstance(
            instance_id=instance_id,
            role=role,
            endpoint=endpoint,
        )
        self._instances[instance_id] = instance
        self._role_instances[role.name].append(instance_id)
        return instance

    def _remove_instance(self, instance_id: str) -> None:
        """Remove an instance from all indexes."""
        inst = self._instances.pop(instance_id, None)
        if inst is None:
            return
        role_name = inst.role.name
        ids = self._role_instances.get(role_name, [])
        if instance_id in ids:
            ids.remove(instance_id)
