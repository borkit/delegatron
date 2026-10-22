"""Tests for cost tracking."""

from pathlib import Path

import pytest

from delegatron.config import PRESETS, Config, RoleConfig, is_local_model
from delegatron.utils.cost import PRICING, CostTracker, logged_cost_usd


def _cloud_models() -> set[str]:
    """Every cloud model the presets and the built-in role defaults name."""
    roles = [
        RoleConfig(
            provider=str(entry["provider"]),
            model=str(entry["model"]),
            base_url=str(entry.get("base_url", "")),
        )
        for preset in PRESETS.values()
        for entry in preset.values()
        if isinstance(entry, dict) and "model" in entry
    ]
    defaults = Config()
    roles += [defaults.architect, defaults.implementer, defaults.reviewer]
    return {role.model for role in roles if not is_local_model(role)}


@pytest.mark.parametrize("model", sorted(_cloud_models()))
def test_every_preset_cloud_model_is_priced(model):
    """An unpriced model is billed at the fallback guess, which can end a run early."""
    assert model in PRICING


def test_cost_tracker_records():
    tracker = CostTracker()
    cost = tracker.record("gpt-4.1", input_tokens=1000, output_tokens=500)

    assert cost > 0
    assert tracker.total_input_tokens == 1000
    assert tracker.total_output_tokens == 500
    assert tracker.total_cost_usd == cost
    assert len(tracker.calls) == 1


def test_cost_tracker_accumulates():
    tracker = CostTracker()
    tracker.record("gpt-4.1", input_tokens=1000, output_tokens=500)
    tracker.record("gemini-2.5-pro", input_tokens=2000, output_tokens=1000)

    assert tracker.total_input_tokens == 3000
    assert tracker.total_output_tokens == 1500
    assert len(tracker.calls) == 2


def test_cost_tracker_with_request_ids():
    tracker = CostTracker()
    tracker.record(
        "gpt-4.1",
        input_tokens=1000,
        output_tokens=500,
        request_id="dlg-abc123",
        provider_request_id="chatcmpl-xyz",
        role="implementer",
        iteration=2,
    )

    call = tracker.calls[0]
    assert call.request_id == "dlg-abc123"
    assert call.provider_request_id == "chatcmpl-xyz"
    assert call.role == "implementer"
    assert call.iteration == 2


def test_cost_tracker_log_file(tmp_path: Path):
    log_file = tmp_path / "calls.jsonl"
    tracker = CostTracker()
    tracker.set_log_file(log_file)

    tracker.record("gpt-4.1", input_tokens=100, output_tokens=50, role="architect")
    tracker.record("gemini-2.5-pro", input_tokens=200, output_tokens=100, role="reviewer")

    assert log_file.exists()
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2

    import json

    first = json.loads(lines[0])
    assert first["role"] == "architect"
    assert first["model"] == "gpt-4.1"
    assert "timestamp" in first
    assert "request_id" in first


def test_cost_tracker_estimate():
    tracker = CostTracker()
    est = tracker.estimate_cost("gpt-4.1", input_tokens=1_000_000, output_tokens=500_000)
    # gpt-4.1: $2/M input + $8/M output = $2 + $4 = $6
    assert abs(est - 6.0) < 0.01
    # Should NOT record the call
    assert len(tracker.calls) == 0


def test_cost_tracker_summary():
    tracker = CostTracker()
    tracker.record("gpt-4.1", input_tokens=1000, output_tokens=500, role="implementer", iteration=1)
    tracker.record(
        "gemini-2.5-pro",
        input_tokens=2000,
        output_tokens=1000,
        role="reviewer",
        iteration=1,
    )
    tracker.record("gpt-4.1", input_tokens=1000, output_tokens=500, role="implementer", iteration=2)

    summary = tracker.get_summary()
    assert summary["total_calls"] == 3
    assert summary["total_input_tokens"] == 4000
    assert "implementer" in summary["by_role"]
    assert summary["by_role"]["implementer"]["calls"] == 2
    assert "reviewer" in summary["by_role"]
    assert summary["by_role"]["reviewer"]["calls"] == 1


def test_call_record_to_dict():
    tracker = CostTracker()
    tracker.record("gpt-4.1", input_tokens=100, output_tokens=50, role="architect")
    d = tracker.calls[0].to_dict()
    assert d["model"] == "gpt-4.1"
    assert d["role"] == "architect"
    assert "timestamp" in d
    assert "request_id" in d


def test_prior_cost_is_carried_into_the_cumulative_total():
    tracker = CostTracker(prior_cost_usd=4.8)
    cost = tracker.record("gpt-4.1", input_tokens=1000, output_tokens=500, role="implementer")

    assert tracker.cumulative_cost_usd == 4.8 + cost
    summary = tracker.get_summary()
    assert summary["total_cost_usd"] == 4.8 + cost
    assert summary["prior_cost_usd"] == 4.8
    assert summary["session_cost_usd"] == cost
    assert summary["by_role"]["implementer"]["cost_usd"] == cost


def test_logged_cost_sums_the_call_log(tmp_path: Path):
    log = tmp_path / "provider_calls.jsonl"
    tracker = CostTracker()
    tracker.set_log_file(log)
    first = tracker.record("gpt-4.1", input_tokens=1000, output_tokens=500)
    second = tracker.record("gpt-4.1", input_tokens=2000, output_tokens=100)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("not json\n")

    assert logged_cost_usd(log) == pytest.approx(first + second)
    assert logged_cost_usd(tmp_path / "missing.jsonl") == 0.0


def test_explicit_locality_overrides_the_model_name():
    tracker = CostTracker()

    local = tracker.record("gpt-oss:20b", 2337, 673, generation_seconds=65, is_local=True)
    cloud = tracker.record("deepseek-chat", 500_000, 200_000, is_local=False)

    assert tracker.calls[0].is_local is True
    assert tracker.calls[0].local_cost is not None
    assert local == tracker.calls[0].local_cost["total_local_usd"]
    assert tracker.calls[1].is_local is False
    assert cloud > 1.0
    assert tracker.estimate_cost("deepseek-chat", 1000, 1000, is_local=True) == 0.0


def test_gpt_oss_name_is_local_without_endpoint_info():
    tracker = CostTracker()
    tracker.record("gpt-oss:20b", 100, 100)

    assert tracker.calls[0].is_local is True


def test_usage_mark_brackets_the_calls_of_a_step():
    tracker = CostTracker()
    tracker.record("gpt-4.1", input_tokens=7, output_tokens=3)
    before = tracker.usage_mark()

    tracker.record("gpt-4.1", input_tokens=100, output_tokens=40, role="implementer")
    tracker.record("gpt-4.1", input_tokens=20, output_tokens=5, role="implementer")
    after = tracker.usage_mark()

    assert before == (7, 3, 1)
    assert tuple(a - b for a, b in zip(after, before, strict=True)) == (120, 45, 2)
    assert tracker.usage_mark() == after  # a step with no provider request adds 0 calls


def test_cache_tokens_are_priced_at_the_cache_rates(tmp_path: Path):
    import json

    tracker = CostTracker()
    tracker.set_log_file(tmp_path / "calls.jsonl")
    # claude-sonnet-4: input $3, cache read $0.30, cache write $3.75, output $15 per M
    cost = tracker.record(
        "claude-sonnet-4-20250514",
        input_tokens=1_000_000,
        output_tokens=0,
        role="architect",
        cache_read_tokens=600_000,
        cache_write_tokens=200_000,
        temperature=0.2,
    )

    assert cost == pytest.approx(0.2 * 3.0 + 0.6 * 0.30 + 0.2 * 3.75)
    assert tracker.total_input_tokens == 1_000_000
    summary = tracker.get_summary()
    assert summary["total_cache_read_tokens"] == 600_000
    assert summary["total_cache_write_tokens"] == 200_000
    assert summary["by_role"]["architect"]["cache_read_tokens"] == 600_000
    assert summary["by_role"]["architect"]["cache_write_tokens"] == 200_000
    row = json.loads((tmp_path / "calls.jsonl").read_text(encoding="utf-8"))
    assert row["cache_read_tokens"] == 600_000
    assert row["cache_write_tokens"] == 200_000
    assert row["temperature"] == 0.2


def test_cache_write_premium_counts_toward_the_run_cost():
    plain = CostTracker()
    cached = CostTracker()
    plain.record("claude-sonnet-4-20250514", input_tokens=10_000, output_tokens=0)
    cached.record(
        "claude-sonnet-4-20250514", input_tokens=10_000, output_tokens=0, cache_write_tokens=10_000
    )

    assert cached.cumulative_cost_usd > plain.cumulative_cost_usd


def test_cache_rates_fall_back_to_the_input_rate():
    tracker = CostTracker()
    with_cache = tracker.record("gpt-4.1", 1000, 0, cache_write_tokens=1000)

    assert with_cache == pytest.approx(tracker.estimate_cost("gpt-4.1", 1000, 0))


def test_cache_read_without_a_cached_rate_is_priced_at_the_input_rate(
    monkeypatch: pytest.MonkeyPatch,
):
    from delegatron.utils import cost as cost_module

    monkeypatch.setitem(cost_module.PRICING, "m-no-cache-rate", {"input": 2.0, "output": 8.0})
    tracker = CostTracker()

    read = tracker.record("m-no-cache-rate", 1000, 0, is_local=False, cache_read_tokens=1000)
    mixed = tracker.record("m-no-cache-rate", 1000, 10, is_local=False, cache_read_tokens=400)

    assert read == pytest.approx(tracker.estimate_cost("m-no-cache-rate", 1000, 0, is_local=False))
    assert read == pytest.approx(1000 * 2.0 / 1_000_000)
    assert mixed == pytest.approx((1000 * 2.0 + 10 * 8.0) / 1_000_000)


def test_cache_read_with_a_cached_rate_is_cheaper_than_input(monkeypatch: pytest.MonkeyPatch):
    from delegatron.utils import cost as cost_module

    rates = {"input": 2.0, "output": 8.0, "cached_input": 0.5}
    monkeypatch.setitem(cost_module.PRICING, "m-cache-rate", rates)
    tracker = CostTracker()

    read = tracker.record("m-cache-rate", 1000, 0, is_local=False, cache_read_tokens=1000)

    assert read == pytest.approx(1000 * 0.5 / 1_000_000)


def test_cache_counts_never_exceed_the_prompt():
    tracker = CostTracker()
    tracker.record("gpt-4.1", input_tokens=10, output_tokens=1, cache_read_tokens=50)

    assert tracker.total_input_tokens == 50


def test_unpriced_cloud_model_warns_once(caplog: pytest.LogCaptureFixture):
    import logging

    from delegatron.utils import cost as cost_module

    logger = logging.getLogger("delegatron.utils.cost")
    logger.addHandler(caplog.handler)
    try:
        tracker = CostTracker()
        cost_module._warned_unpriced.discard("vendor/mystery-model")
        first = tracker.record("vendor/mystery-model", 1_000_000, 0, is_local=False)
        tracker.record("vendor/mystery-model", 10, 10, is_local=False)
        tracker.estimate_cost("vendor/mystery-model", 10, 10, is_local=False)
        tracker.record("qwen3:8b", 10, 10, is_local=True)
    finally:
        logger.removeHandler(caplog.handler)

    assert first == pytest.approx(5.0)
    # the handler may see a record twice (attached here and via propagation)
    warnings = {id(r): r.getMessage() for r in caplog.records if r.levelno == logging.WARNING}
    assert len(warnings) == 1
    assert "vendor/mystery-model" in next(iter(warnings.values()))


def test_priced_cloud_model_behind_a_local_proxy_is_billed_at_its_api_price():
    # A LiteLLM proxy on localhost forwards to the cloud API: the GPU estimate
    # would understate the spend and let a run slip past max_cost_usd.
    tracker = CostTracker()

    cost = tracker.record(
        "claude-sonnet-4-20250514", 1_000_000, 0, generation_seconds=30, is_local=True
    )

    assert cost == pytest.approx(3.0)
    assert tracker.calls[0].local_cost is None
    assert tracker.total_local_cost_usd == 0.0
