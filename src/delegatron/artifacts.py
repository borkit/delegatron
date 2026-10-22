"""Artifact store — persists all pipeline artifacts to disk.

Each run gets a directory under runs/{run_id}/ containing:
- work_plan.json (architect output)
- patch_set_{iteration}.json (implementer outputs)
- test_report_{iteration}.json (sandbox outputs)
- review_report_{iteration}.json (reviewer outputs)
- events.jsonl (append-only event log)
- state.json (full state snapshot per iteration, for resume)
- summary.json (final run summary)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


class ArtifactStore:
    """Manages persistent storage of pipeline artifacts."""

    # Only allow safe run_id characters (hex, hyphens, underscores)
    _RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    def __init__(self, base_dir: Path, run_id: str) -> None:
        if not self._RUN_ID_PATTERN.match(run_id):
            msg = f"Invalid run_id: {run_id!r}"
            raise ValueError(msg)
        run_dir = (base_dir / run_id).resolve()
        if not run_dir.is_relative_to(base_dir.resolve()):
            msg = f"run_id escapes base directory: {run_id!r}"
            raise ValueError(msg)
        self._run_dir = run_dir
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._events_file = self._run_dir / "events.jsonl"

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def save_plan(self, plan: dict[str, Any]) -> Path:
        """Save the architect's WorkPlan."""
        return self._write_json("work_plan.json", plan)

    def save_patch(self, iteration: int, patch: dict[str, Any]) -> Path:
        """Save an implementer's PatchSet."""
        return self._write_json(f"patch_set_{iteration:03d}.json", patch)

    def save_test_report(self, iteration: int, report: dict[str, Any]) -> Path:
        """Save a sandbox TestReport."""
        return self._write_json(f"test_report_{iteration:03d}.json", report)

    def save_review(self, iteration: int, review: dict[str, Any]) -> Path:
        """Save a reviewer's ReviewReport."""
        return self._write_json(f"review_report_{iteration:03d}.json", review)

    def save_state(self, state: dict[str, Any]) -> Path:
        """Save the full pipeline state (for checkpoint/resume)."""
        iteration = state.get("iteration", 0)
        return self._write_json(f"state_{iteration:03d}.json", state)

    def save_summary(self, summary: dict[str, Any]) -> Path:
        """Save the final run summary."""
        return self._write_json("summary.json", summary)

    def append_event(self, event: dict[str, Any]) -> None:
        """Append an event to the JSONL event log."""
        event.setdefault("timestamp", datetime.now(UTC).isoformat())
        with open(self._events_file, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def has_state(self) -> bool:
        """Whether a state checkpoint was saved, i.e. the run can be resumed."""
        return any(self._run_dir.glob("state_*.json"))

    def load_state(self, iteration: int | None = None) -> dict[str, Any] | None:
        """Load a saved state for resume. If iteration is None, load the latest."""
        if iteration is not None:
            path = self._run_dir / f"state_{iteration:03d}.json"
            if path.exists():
                return cast(dict[str, Any], json.loads(path.read_text()))
            return None

        # Find latest state file
        state_files = sorted(self._run_dir.glob("state_*.json"))
        if not state_files:
            return None
        return cast(dict[str, Any], json.loads(state_files[-1].read_text()))

    def load_plan(self) -> dict[str, Any] | None:
        """Load a saved WorkPlan."""
        path = self._run_dir / "work_plan.json"
        if path.exists():
            return cast(dict[str, Any], json.loads(path.read_text()))
        return None

    def list_runs(self) -> list[str]:
        """List all run IDs in the base directory."""
        if not self._run_dir.parent.exists():
            return []
        return sorted(d.name for d in self._run_dir.parent.iterdir() if d.is_dir())

    def _write_json(self, filename: str, data: dict[str, Any]) -> Path:
        """Write a JSON file to the run directory."""
        path = self._run_dir / filename
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.debug("Saved artifact: %s", path)
        return path
