"""Usage analytics — tracks and aggregates pipeline run metrics."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """A single pipeline run's usage data."""

    run_id: str
    preset: str
    request_summary: str
    status: str  # "approved", "exhausted", "failed"
    iterations: int
    cost_usd: float
    duration_seconds: float
    timestamp: str = ""
    user_id: str | None = None
    model_usage: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> UsageRecord:
        filtered: dict[str, Any] = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


@dataclass
class UsageSummary:
    """Aggregated usage statistics."""

    total_runs: int
    total_cost_usd: float
    avg_cost_per_run: float
    avg_iterations: float
    success_rate: float
    most_used_preset: str
    runs_by_status: dict[str, int] = field(default_factory=dict)
    tokens_by_model: dict[str, float] = field(default_factory=dict)


class AnalyticsStore:
    """Persists and queries usage records as JSONL."""

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, usage: UsageRecord) -> None:
        """Append a usage record to the JSONL store."""
        with open(self._store_path, "a") as f:
            f.write(json.dumps(usage.to_dict(), default=str) + "\n")
        logger.debug("Recorded analytics for run %s", usage.run_id)

    def query(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        user_id: str | None = None,
    ) -> list[UsageRecord]:
        """Query records with optional time range and user filter."""
        records: list[UsageRecord] = []

        if not self._store_path.exists():
            return records

        with open(self._store_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                rec = UsageRecord.from_dict(data)

                if user_id is not None and rec.user_id != user_id:
                    continue

                if start is not None or end is not None:
                    rec_ts = datetime.fromisoformat(rec.timestamp)
                    if start is not None and rec_ts < start:
                        continue
                    if end is not None and rec_ts > end:
                        continue

                records.append(rec)

        return records

    def summary(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> UsageSummary:
        """Compute aggregated statistics over a time range."""
        records = self.query(start=start, end=end)

        if not records:
            return UsageSummary(
                total_runs=0,
                total_cost_usd=0.0,
                avg_cost_per_run=0.0,
                avg_iterations=0.0,
                success_rate=0.0,
                most_used_preset="",
            )

        total_runs = len(records)
        total_cost = sum(r.cost_usd for r in records)
        total_iterations = sum(r.iterations for r in records)
        approved = sum(1 for r in records if r.status == "approved")

        runs_by_status: dict[str, int] = {}
        preset_counts: dict[str, int] = {}
        tokens_by_model: dict[str, float] = {}

        for rec in records:
            runs_by_status[rec.status] = runs_by_status.get(rec.status, 0) + 1
            preset_counts[rec.preset] = preset_counts.get(rec.preset, 0) + 1
            for model, tokens in rec.model_usage.items():
                tokens_by_model[model] = tokens_by_model.get(model, 0.0) + tokens

        most_used_preset = max(preset_counts, key=preset_counts.get) if preset_counts else ""  # type: ignore[arg-type]

        return UsageSummary(
            total_runs=total_runs,
            total_cost_usd=total_cost,
            avg_cost_per_run=total_cost / total_runs,
            avg_iterations=total_iterations / total_runs,
            success_rate=approved / total_runs,
            most_used_preset=most_used_preset,
            runs_by_status=runs_by_status,
            tokens_by_model=tokens_by_model,
        )
