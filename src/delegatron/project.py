"""Project mode — multi-milestone orchestrator for building complete projects.

Takes a high-level project description ("build a SaaS task management app
with auth, teams, and billing") and decomposes it into milestones. Each
milestone is run through the standard pipeline (architect → implement →
test → review). Milestones are sequential — each builds on previous ones.

Usage:
    # CLI
    delegatron project "Build a Flask todo app with auth, database, and tests"
    delegatron project --preset local --max-milestones 10 "Build a REST API"

    # Python
    runner = ProjectRunner(config=config, workspace_root=Path("."))
    report = await runner.execute("Build a complete todo app")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from delegatron.artifacts import ArtifactStore
from delegatron.config import Config
from delegatron.orchestrator.graph import Pipeline, task_outcome_rows
from delegatron.orchestrator.scoring import compute_score
from delegatron.prompts import load_prompt

logger = logging.getLogger(__name__)

# Default project limits
DEFAULT_MAX_MILESTONES = 10
DEFAULT_MAX_ITERATIONS_PER_MILESTONE = 5
DEFAULT_MAX_COST_PER_MILESTONE = 5.0
DEFAULT_MAX_TOTAL_COST = 50.0
DEFAULT_MAX_WALL_TIME_MINUTES = 120


@dataclass
class Milestone:
    """A single milestone in a project plan."""

    id: str
    title: str
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "acceptance_criteria": self.acceptance_criteria,
            "depends_on": self.depends_on,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Milestone:
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            acceptance_criteria=d.get("acceptance_criteria", []),
            depends_on=d.get("depends_on", []),
            order=d.get("order", 0),
        )


@dataclass
class MilestoneResult:
    """Result of executing a single milestone."""

    milestone_id: str
    title: str
    status: str  # approved, partial, exhausted, stalled, error
    iterations: int
    cost_usd: float
    elapsed_seconds: float
    emcs_score: float
    files_created: list[str] = field(default_factory=list)
    error: str = ""
    # How each task of the milestone's plan ended: {task_id, title, status, iterations}
    task_outcomes: list[dict[str, Any]] = field(default_factory=list)
    # The run branch the milestone's work is on ("" when it has none, e.g. no git)
    branch: str = ""
    # Files the milestone deleted
    files_deleted: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "title": self.title,
            "status": self.status,
            "iterations": self.iterations,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "emcs_score": round(self.emcs_score, 3),
            "files_created": self.files_created,
            "files_deleted": self.files_deleted,
            "error": self.error,
            "task_outcomes": self.task_outcomes,
            "branch": self.branch,
        }


@dataclass
class ProjectPlan:
    """The meta-architect's decomposition of a project."""

    summary: str
    milestones: list[Milestone] = field(default_factory=list)
    total_estimated_iterations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "milestones": [m.to_dict() for m in self.milestones],
            "total_estimated_iterations": self.total_estimated_iterations,
        }


@dataclass
class ProjectReport:
    """Final report for a complete project run."""

    project_id: str
    request: str
    preset: str
    plan: ProjectPlan | None = None
    results: list[MilestoneResult] = field(default_factory=list)
    status: str = "running"  # running, completed, failed, cancelled
    total_cost_usd: float = 0.0
    total_time_seconds: float = 0.0
    started_at: str = ""
    completed_at: str = ""
    error: str = ""

    @property
    def milestones_completed(self) -> int:
        return sum(1 for r in self.results if r.status == "approved")

    @property
    def milestones_total(self) -> int:
        return len(self.plan.milestones) if self.plan else 0

    @property
    def success_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.milestones_completed / len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "request": self.request,
            "preset": self.preset,
            "status": self.status,
            "plan": self.plan.to_dict() if self.plan else None,
            "results": [r.to_dict() for r in self.results],
            "milestones_completed": self.milestones_completed,
            "milestones_total": self.milestones_total,
            "success_rate": round(self.success_rate, 3),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_time_seconds": round(self.total_time_seconds, 1),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }


# The meta-architect prompt for decomposing projects into milestones
# (prompts/meta_architect/v<N>.md, never hardcoded).
META_ARCHITECT_PROMPT = "meta_architect"
META_ARCHITECT_PROMPT_VERSION = 1


def _run_branch(state: dict[str, Any] | Any) -> str:
    """The run branch a finished pipeline worked on ("" when it set none up)."""
    for message in reversed(state.get("messages") or []):
        if isinstance(message, dict) and message.get("event") == "workspace_initialized":
            return str(message.get("branch") or "")
    return ""


class ProjectRunner:
    """Orchestrates a multi-milestone project through sequential pipeline runs.

    Each milestone runs through the full Delegatron pipeline
    (architect → implement → test → review). The workspace accumulates
    code across milestones — milestone 3 sees everything built by 1 and 2.
    """

    def __init__(
        self,
        config: Config,
        workspace_root: Path,
        project_id: str,
        artifacts_dir: Path | None = None,
        max_milestones: int = DEFAULT_MAX_MILESTONES,
        max_iterations_per_milestone: int = DEFAULT_MAX_ITERATIONS_PER_MILESTONE,
        max_cost_per_milestone: float = DEFAULT_MAX_COST_PER_MILESTONE,
        max_total_cost: float = DEFAULT_MAX_TOTAL_COST,
        max_wall_time_minutes: float = DEFAULT_MAX_WALL_TIME_MINUTES,
        step_callback: Any | None = None,
        allow_dirty: bool = False,
        base_branch: str = "",
    ) -> None:
        self.config = config
        self.workspace_root = workspace_root
        self.project_id = project_id
        self.artifacts_dir = artifacts_dir or (workspace_root / "runs")
        self.max_milestones = max_milestones
        self.max_iterations = max_iterations_per_milestone
        self.max_cost_per_milestone = max_cost_per_milestone
        self.max_total_cost = max_total_cost
        self.max_wall_time = max_wall_time_minutes
        self.step_callback = step_callback
        # The first milestone may start on a dirty workspace only when asked to;
        # later ones always start from the previous milestone's output.
        self.allow_dirty = allow_dirty
        # Branch the first milestone forks from ("" = the checked-out branch).
        self.base_branch = base_branch

    def _notify(self, event: str, data: dict[str, Any]) -> None:
        import contextlib

        if self.step_callback:
            with contextlib.suppress(Exception):
                self.step_callback(event, data)

    async def execute(self, request: str) -> ProjectReport:
        """Execute a full project from a natural language description.

        Steps:
        1. Meta-architect decomposes project into milestones
        2. For each milestone, run the standard pipeline
        3. Each milestone builds on the accumulated workspace
        4. Track costs, time, and results across all milestones
        """
        project_start = time.monotonic()
        report = ProjectReport(
            project_id=self.project_id,
            request=request,
            preset=self._get_preset_name(),
            started_at=datetime.now(UTC).isoformat(),
        )

        # Set up project-level artifact store
        store = ArtifactStore(base_dir=self.artifacts_dir, run_id=self.project_id)
        store.append_event(
            {
                "event": "project_start",
                "request": request,
                "project_id": self.project_id,
            }
        )

        self._notify(
            "project_start",
            {
                "project_id": self.project_id,
                "request": request[:200],
            },
        )

        # Step 1: Meta-architect — decompose into milestones
        try:
            plan = await self._create_project_plan(request)
        except Exception as e:
            report.status = "failed"
            report.error = f"Project planning failed: {e}"
            report.completed_at = datetime.now(UTC).isoformat()
            report.total_time_seconds = time.monotonic() - project_start
            self._save_report(store, report)
            return report

        report.plan = plan

        # Limit milestones
        milestones = plan.milestones[: self.max_milestones]

        logger.info(
            "Project plan: %s (%d milestones)",
            plan.summary,
            len(milestones),
        )
        self._notify(
            "project_planned",
            {
                "summary": plan.summary,
                "milestones": len(milestones),
                "titles": [m.title for m in milestones],
            },
        )

        store.append_event(
            {
                "event": "project_planned",
                "summary": plan.summary,
                "milestones": [m.to_dict() for m in milestones],
            }
        )
        store.save_plan(plan.to_dict())

        # Step 2: Execute each milestone through the pipeline
        total_cost = 0.0
        # Track the current branch so each milestone builds on the previous one.
        # The first milestone starts from ``base_branch`` (default: the branch
        # checked out); later ones from the previous milestone's run branch.
        current_branch = self.base_branch
        for i, milestone in enumerate(milestones):
            # Check total cost budget
            if total_cost >= self.max_total_cost:
                logger.warning(
                    "Total cost budget exceeded ($%.2f >= $%.2f), stopping",
                    total_cost,
                    self.max_total_cost,
                )
                report.status = "failed"
                report.error = f"Total cost budget exceeded: ${total_cost:.2f}"
                break

            # Check wall time
            elapsed = time.monotonic() - project_start
            if elapsed / 60 >= self.max_wall_time:
                logger.warning("Wall time limit reached (%.0fm)", elapsed / 60)
                report.status = "failed"
                report.error = f"Wall time limit reached: {elapsed / 60:.0f}m"
                break

            logger.info(
                "=== Milestone %d/%d: %s ===",
                i + 1,
                len(milestones),
                milestone.title,
            )
            self._notify(
                "milestone_start",
                {
                    "milestone": i + 1,
                    "total": len(milestones),
                    "title": milestone.title,
                    "description": milestone.description[:200],
                },
            )

            store.append_event(
                {
                    "event": "milestone_start",
                    "milestone_id": milestone.id,
                    "title": milestone.title,
                    "order": i + 1,
                }
            )

            # Run the pipeline for this milestone (each builds on the previous)
            result = await self._run_milestone(milestone, i, store, base_branch=current_branch)
            report.results.append(result)
            total_cost += result.cost_usd

            # Next milestone starts from this milestone's branch
            if result.status not in ("error",) and result.branch:
                current_branch = result.branch
            report.total_cost_usd = total_cost

            self._notify(
                "milestone_complete",
                {
                    "milestone": i + 1,
                    "total": len(milestones),
                    "title": milestone.title,
                    "status": result.status,
                    "cost": result.cost_usd,
                    "iterations": result.iterations,
                },
            )

            store.append_event(
                {
                    "event": "milestone_complete",
                    "milestone_id": milestone.id,
                    "status": result.status,
                    "cost_usd": result.cost_usd,
                    "iterations": result.iterations,
                }
            )

            # If milestone failed with error, stop the project
            if result.status == "error":
                logger.error(
                    "Milestone %s failed with error: %s",
                    milestone.id,
                    result.error,
                )
                report.status = "failed"
                report.error = f"Milestone '{milestone.title}' failed: {result.error}"
                break

            # Save incremental progress
            self._save_report(store, report)

            logger.info(
                "Milestone %d/%d: %s (%s, %d iter, $%.4f)",
                i + 1,
                len(milestones),
                milestone.title,
                result.status,
                result.iterations,
                result.cost_usd,
            )

        # Finalize
        if report.status == "running":
            all_approved = all(r.status == "approved" for r in report.results)
            report.status = "completed" if all_approved else "completed_partial"

        report.total_time_seconds = time.monotonic() - project_start
        report.completed_at = datetime.now(UTC).isoformat()

        self._save_report(store, report)

        store.append_event(
            {
                "event": "project_complete",
                "status": report.status,
                "milestones_completed": report.milestones_completed,
                "milestones_total": report.milestones_total,
                "total_cost_usd": report.total_cost_usd,
                "total_time_seconds": report.total_time_seconds,
            }
        )

        self._notify(
            "project_complete",
            {
                "status": report.status,
                "milestones_completed": report.milestones_completed,
                "milestones_total": report.milestones_total,
                "cost": report.total_cost_usd,
                "time": report.total_time_seconds,
            },
        )

        logger.info(
            "Project complete: %s (%d/%d milestones, $%.4f, %.0fs)",
            report.status,
            report.milestones_completed,
            report.milestones_total,
            report.total_cost_usd,
            report.total_time_seconds,
        )

        return report

    async def _create_project_plan(self, request: str) -> ProjectPlan:
        """Use the architect model to decompose a project into milestones."""
        from delegatron.agents.providers.registry import get_provider

        provider = get_provider(self.config.architect)

        # Build the planning prompt
        user_message = (
            f"Decompose this project into milestones:\n\n{request}\n\n"
            f"Maximum milestones: {self.max_milestones}\n"
            f"Each milestone should be implementable in {self.max_iterations} "
            f"iterations or fewer."
        )

        response = await provider.generate(
            system_prompt=load_prompt(META_ARCHITECT_PROMPT, version=META_ARCHITECT_PROMPT_VERSION),
            user_message=user_message,
            max_tokens=self.config.architect.max_tokens,
            temperature=self.config.architect.temperature,
        )

        # Parse the response
        import re

        content = response.content.strip()
        # Strip <think> blocks if present
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        # Strip markdown fences
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [ln for ln in lines[1:] if not ln.startswith("```")]
            content = "\n".join(lines).strip()

        data = json.loads(content)
        milestones = []
        for i, m in enumerate(data.get("milestones", [])):
            milestones.append(
                Milestone(
                    id=m.get("id", f"m{i + 1}"),
                    title=m.get("title", f"Milestone {i + 1}"),
                    description=m.get("description", ""),
                    acceptance_criteria=m.get("acceptance_criteria", []),
                    depends_on=m.get("depends_on", []),
                    order=m.get("order", i + 1),
                )
            )

        if not milestones:
            raise ValueError(
                "Architect returned an empty milestone plan — cannot proceed with zero milestones"
            )

        return ProjectPlan(
            summary=data.get("summary", ""),
            milestones=milestones,
            total_estimated_iterations=len(milestones) * self.max_iterations,
        )

    async def _run_milestone(
        self,
        milestone: Milestone,
        index: int,
        store: ArtifactStore,
        base_branch: str = "",
    ) -> MilestoneResult:
        """Run a single milestone through the standard pipeline.

        Args:
            base_branch: The git branch to start from. For milestone 1 this is
                ``self.base_branch`` ("" = the checked-out branch); for later
                milestones it's the previous milestone's branch so code accumulates.
        """
        from dataclasses import replace

        start = time.monotonic()

        try:
            # Configure pipeline for this milestone
            config = replace(
                self.config,
                pipeline=replace(
                    self.config.pipeline,
                    max_iterations=self.max_iterations,
                    max_cost_usd=self.max_cost_per_milestone,
                ),
            )

            run_id = f"{self.project_id}-m{index + 1}"

            pipeline = Pipeline(
                config=config,
                workspace_root=self.workspace_root,
                run_id=run_id,
                artifacts_dir=self.artifacts_dir,
                base_branch=base_branch,
                step_callback=self.step_callback,
                # Later milestones start from the previous milestone's output,
                # including its uncommitted sandbox byproducts.
                allow_dirty=self.allow_dirty or index > 0,
            )

            # The milestone description is the task request
            state = await pipeline.execute(milestone.description, stream=True)
            elapsed = time.monotonic() - start

            # Extract results
            review = state.get("review_report") or {}
            test_report = state.get("test_report")
            emcs = compute_score(review, test_report) if review else 0.0

            # Every file the milestone changed, not only the last patch's files
            history: dict[str, dict[str, Any]] = state.get("_file_history") or {}
            files = sorted(path for path, entry in history.items() if not entry.get("deleted"))
            deleted = sorted(path for path, entry in history.items() if entry.get("deleted"))

            return MilestoneResult(
                milestone_id=milestone.id,
                title=milestone.title,
                status=state.get("status", "unknown"),
                iterations=state.get("iteration", 0),
                cost_usd=state.get("total_cost_usd", 0.0),
                elapsed_seconds=round(elapsed, 1),
                emcs_score=emcs,
                files_created=files,
                files_deleted=deleted,
                task_outcomes=task_outcome_rows(state),
                branch=_run_branch(state),
            )

        except Exception as e:
            elapsed = time.monotonic() - start
            return MilestoneResult(
                milestone_id=milestone.id,
                title=milestone.title,
                status="error",
                iterations=0,
                cost_usd=0.0,
                elapsed_seconds=round(elapsed, 1),
                emcs_score=0.0,
                error=str(e),
            )

    def _save_report(self, store: ArtifactStore, report: ProjectReport) -> None:
        """Save the project report to disk."""
        store.save_summary(report.to_dict())

    def _get_preset_name(self) -> str:
        """Derive a preset name from the config for display."""
        model = self.config.architect.model
        if "claude" in model:
            return "claude"
        if "gpt" in model:
            return "openai"
        if "gemini" in model:
            return "google"
        if "qwen" in model or "deepseek" in model:
            return "ollama"
        return "custom"
