"""Tests for the analytics module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from delegatron.analytics import AnalyticsStore, UsageRecord


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "analytics.jsonl"


@pytest.fixture
def store(store_path: Path) -> AnalyticsStore:
    return AnalyticsStore(store_path=store_path)


def _make_record(
    run_id: str = "run-001",
    status: str = "approved",
    cost: float = 1.0,
    iterations: int = 3,
    preset: str = "default",
    user_id: str | None = None,
    timestamp: str = "",
    model_usage: dict[str, int] | None = None,
) -> UsageRecord:
    return UsageRecord(
        run_id=run_id,
        preset=preset,
        request_summary="test task",
        status=status,
        iterations=iterations,
        cost_usd=cost,
        duration_seconds=10.0,
        user_id=user_id,
        timestamp=timestamp,
        model_usage=model_usage or {},
    )


class TestUsageRecord:
    def test_auto_timestamp(self) -> None:
        rec = _make_record()
        assert rec.timestamp != ""
        # Should parse as ISO format
        datetime.fromisoformat(rec.timestamp)

    def test_roundtrip(self) -> None:
        rec = _make_record(model_usage={"gpt-4.1": 5000})
        data = rec.to_dict()
        restored = UsageRecord.from_dict(data)
        assert restored.run_id == rec.run_id
        assert restored.model_usage == {"gpt-4.1": 5000}

    def test_explicit_timestamp_preserved(self) -> None:
        ts = "2026-01-01T00:00:00+00:00"
        rec = _make_record(timestamp=ts)
        assert rec.timestamp == ts


class TestAnalyticsStore:
    def test_record_and_query_empty(self, store: AnalyticsStore) -> None:
        assert store.query() == []

    def test_record_and_query(self, store: AnalyticsStore) -> None:
        rec = _make_record()
        store.record(rec)
        results = store.query()
        assert len(results) == 1
        assert results[0].run_id == "run-001"

    def test_query_by_user(self, store: AnalyticsStore) -> None:
        store.record(_make_record(run_id="r1", user_id="alice"))
        store.record(_make_record(run_id="r2", user_id="bob"))
        store.record(_make_record(run_id="r3", user_id="alice"))

        results = store.query(user_id="alice")
        assert len(results) == 2
        assert all(r.user_id == "alice" for r in results)

    def test_query_by_time_range(self, store: AnalyticsStore) -> None:
        now = datetime.now(UTC)
        old = (now - timedelta(hours=2)).isoformat()
        recent = (now - timedelta(minutes=30)).isoformat()

        store.record(_make_record(run_id="old", timestamp=old))
        store.record(_make_record(run_id="recent", timestamp=recent))

        results = store.query(start=now - timedelta(hours=1))
        assert len(results) == 1
        assert results[0].run_id == "recent"

        results = store.query(end=now - timedelta(hours=1))
        assert len(results) == 1
        assert results[0].run_id == "old"

    def test_query_time_range_both_bounds(self, store: AnalyticsStore) -> None:
        now = datetime.now(UTC)
        ts1 = (now - timedelta(hours=3)).isoformat()
        ts2 = (now - timedelta(hours=1)).isoformat()
        ts3 = now.isoformat()

        store.record(_make_record(run_id="r1", timestamp=ts1))
        store.record(_make_record(run_id="r2", timestamp=ts2))
        store.record(_make_record(run_id="r3", timestamp=ts3))

        results = store.query(
            start=now - timedelta(hours=2),
            end=now - timedelta(minutes=30),
        )
        assert len(results) == 1
        assert results[0].run_id == "r2"

    def test_multiple_records(self, store: AnalyticsStore) -> None:
        for i in range(5):
            store.record(_make_record(run_id=f"run-{i}"))
        assert len(store.query()) == 5


class TestUsageSummary:
    def test_empty_summary(self, store: AnalyticsStore) -> None:
        summary = store.summary()
        assert summary.total_runs == 0
        assert summary.total_cost_usd == 0.0
        assert summary.success_rate == 0.0
        assert summary.most_used_preset == ""

    def test_summary_aggregation(self, store: AnalyticsStore) -> None:
        store.record(
            _make_record(
                run_id="r1",
                status="approved",
                cost=2.0,
                iterations=3,
                preset="default",
                model_usage={"gpt-4.1": 1000},
            )
        )
        store.record(
            _make_record(
                run_id="r2",
                status="failed",
                cost=1.0,
                iterations=5,
                preset="budget",
                model_usage={"gpt-4.1": 500, "gemini-2.5-pro": 200},
            )
        )
        store.record(
            _make_record(
                run_id="r3",
                status="approved",
                cost=3.0,
                iterations=2,
                preset="default",
                model_usage={"gemini-2.5-pro": 300},
            )
        )

        summary = store.summary()
        assert summary.total_runs == 3
        assert summary.total_cost_usd == pytest.approx(6.0)
        assert summary.avg_cost_per_run == pytest.approx(2.0)
        assert summary.avg_iterations == pytest.approx(10.0 / 3.0)
        assert summary.success_rate == pytest.approx(2.0 / 3.0)
        assert summary.most_used_preset == "default"
        assert summary.runs_by_status == {"approved": 2, "failed": 1}
        assert summary.tokens_by_model["gpt-4.1"] == 1500
        assert summary.tokens_by_model["gemini-2.5-pro"] == 500

    def test_summary_with_time_filter(self, store: AnalyticsStore) -> None:
        now = datetime.now(UTC)
        store.record(
            _make_record(
                run_id="old",
                timestamp=(now - timedelta(hours=2)).isoformat(),
                preset="default",
            )
        )
        store.record(
            _make_record(
                run_id="new",
                timestamp=now.isoformat(),
                preset="budget",
            )
        )

        summary = store.summary(start=now - timedelta(hours=1))
        assert summary.total_runs == 1
        assert summary.most_used_preset == "budget"
