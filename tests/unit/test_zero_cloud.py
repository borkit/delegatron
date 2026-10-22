"""Tests for zero-cloud verification — the key differentiator."""

from __future__ import annotations

import json

import pytest

from delegatron.security.zero_cloud import (
    LOCAL_HOSTS_ENV,
    NetworkGuard,
    _is_local_url,
    is_local_host,
    report_fingerprint,
)


class TestIsLocalUrl:
    def test_localhost(self) -> None:
        assert _is_local_url("http://localhost:11434/v1")

    def test_127_0_0_1(self) -> None:
        assert _is_local_url("http://127.0.0.1:8000/api")

    def test_docker_internal(self) -> None:
        assert _is_local_url("http://host.docker.internal:11434/v1")

    def test_private_10(self) -> None:
        assert _is_local_url("http://10.0.0.5:8080/v1")

    def test_private_192(self) -> None:
        assert _is_local_url("http://192.168.1.100:11434/v1")

    def test_private_172(self) -> None:
        assert _is_local_url("http://172.16.0.1:8000/api")

    def test_cloud_url_is_not_local(self) -> None:
        assert not _is_local_url("https://api.openai.com/v1")

    def test_anthropic_is_not_local(self) -> None:
        assert not _is_local_url("https://api.anthropic.com/v1")

    def test_empty_url(self) -> None:
        assert not _is_local_url("")

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.evil.example.com/v1",
            "http://192.168.attacker.com",
            "http://172.32.0.1",
            "http://8.8.8.8",
            "http://localhost.example.com:11434",
        ],
    )
    def test_not_local(self, url: str) -> None:
        assert not _is_local_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://ollama:11434/v1",
            "http://gpu.local:11434",
            "http://ollama.default.svc.cluster.local:11434",
            "http://127.0.0.2:11434",
            "http://[fd00::1]:11434",
            "http://[::1]:11434",
            "http://10.0.0.5:8080",
            "http://172.16.0.1",
            "http://0.0.0.0:8000",
            "localhost:11434/v1",
        ],
    )
    def test_local(self, url: str) -> None:
        assert _is_local_url(url)

    def test_allowlisted_fqdn_is_local(self) -> None:
        assert not is_local_host("gpu.lab.example.com")
        assert is_local_host("GPU.lab.example.com.", local_hosts={"gpu.lab.example.com"})


class TestNetworkGuardLocalPreset:
    """Test with a fully local Ollama-style configuration."""

    def _make_local_guard(self) -> NetworkGuard:
        guard = NetworkGuard(run_id="test-local-001")
        # Simulate 3 local LLM calls
        guard.record_llm_call(
            role="architect",
            provider="openai-compatible",
            base_url="http://localhost:11434/v1",
            model="qwen3-coder:30b",
        )
        guard.record_llm_call(
            role="implementer",
            provider="openai-compatible",
            base_url="http://localhost:11434/v1",
            model="qwen3-coder:30b",
        )
        guard.record_llm_call(
            role="reviewer",
            provider="openai-compatible",
            base_url="http://localhost:11434/v1",
            model="deepseek-r1:14b",
        )
        guard.record_sandbox_call(sandbox_type="docker")
        guard.record_artifact_store(store_type="local_fs", path="/data/runs/test")
        return guard

    def test_cloud_free_verified(self) -> None:
        guard = self._make_local_guard()
        report = guard.finalize()
        assert report.cloud_free is True

    def test_no_violations(self) -> None:
        guard = self._make_local_guard()
        report = guard.finalize()
        assert len(report.violations) == 0

    def test_has_fingerprint(self) -> None:
        guard = self._make_local_guard()
        report = guard.finalize()
        assert len(report.fingerprint) == 64  # SHA-256 hex

    def test_event_count(self) -> None:
        guard = self._make_local_guard()
        report = guard.finalize()
        assert report.summary["total_events"] == 5
        assert report.summary["local_events"] == 5
        assert report.summary["cloud_events"] == 0

    def test_report_serializable(self) -> None:
        guard = self._make_local_guard()
        report = guard.finalize()
        d = report.to_dict()
        assert d["cloud_free"] is True
        assert d["run_id"] == "test-local-001"
        assert len(d["events"]) == 5

    def test_summary_by_category(self) -> None:
        guard = self._make_local_guard()
        report = guard.finalize()
        assert report.summary["by_category"]["llm_call"] == 3
        assert report.summary["by_category"]["sandbox"] == 1
        assert report.summary["by_category"]["artifact_store"] == 1


class TestNetworkGuardCloudPreset:
    """Test with cloud provider configurations."""

    def test_cloud_preset_reports_violations(self) -> None:
        guard = NetworkGuard(run_id="test-cloud-001")
        guard.record_llm_call(
            role="architect",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
        )
        guard.record_llm_call(
            role="implementer",
            provider="openai",
            model="gpt-4.1",
        )
        guard.record_llm_call(
            role="reviewer",
            provider="google",
            model="gemini-2.5-pro",
        )
        guard.record_sandbox_call(sandbox_type="docker")
        report = guard.finalize()
        assert report.cloud_free is False
        assert len(report.violations) == 3  # 3 cloud LLM calls

    def test_mixed_preset_reports_partial_violations(self) -> None:
        guard = NetworkGuard(run_id="test-mixed-001")
        guard.record_llm_call(
            role="architect",
            provider="openai-compatible",
            base_url="http://localhost:11434/v1",
            model="qwen3-coder:30b",
        )
        guard.record_llm_call(
            role="implementer",
            provider="openai",
            model="gpt-4.1",
        )
        guard.record_sandbox_call(sandbox_type="docker")
        report = guard.finalize()
        assert report.cloud_free is False
        assert len(report.violations) == 1  # Only implementer is cloud

    def test_e2b_sandbox_is_cloud(self) -> None:
        guard = NetworkGuard(run_id="test-e2b-001")
        guard.record_llm_call(
            role="architect",
            provider="openai-compatible",
            base_url="http://localhost:11434/v1",
            model="qwen3.5:9b",
        )
        guard.record_sandbox_call(sandbox_type="e2b")
        report = guard.finalize()
        assert report.cloud_free is False
        assert len(report.violations) == 1
        assert report.violations[0]["category"] == "sandbox"
        assert report.events[1].is_local is False

    @pytest.mark.parametrize(
        ("sandbox", "local"),
        [("docker", True), ("local", True), ("none", True), ("e2b", False), ("mystery", False)],
    )
    def test_sandbox_locality_follows_type(self, sandbox: str, local: bool) -> None:
        guard = NetworkGuard(run_id="x")
        guard.record_sandbox_call(sandbox)
        assert guard.finalize().cloud_free is local


class TestExternalCalls:
    def test_git_push_to_github_breaks_zero_cloud(self) -> None:
        guard = NetworkGuard(run_id="pr")
        guard.record_sandbox_call("docker")
        guard.record_external_call("git_push", "github.com")
        report = guard.finalize()
        assert report.cloud_free is False
        assert [(v["category"], v["host"]) for v in report.violations] == [
            ("git_push", "github.com")
        ]

    def test_e2b_is_never_local(self) -> None:
        guard = NetworkGuard(run_id="e2b")
        guard.record_external_call("e2b", "localhost")
        assert guard.finalize().cloud_free is False

    def test_push_to_lan_git_host_is_local(self) -> None:
        guard = NetworkGuard(run_id="lan")
        guard.record_external_call("git_push", "192.168.1.20")
        assert guard.finalize().cloud_free is True

    def test_empty_host_is_not_local(self) -> None:
        guard = NetworkGuard(run_id="x")
        guard.record_external_call("notification", "")
        report = guard.finalize()
        assert report.cloud_free is False
        assert report.events[0].host == "unknown"


class TestNetworkGuardEdgeCases:
    def test_empty_guard(self) -> None:
        guard = NetworkGuard(run_id="test-empty")
        report = guard.finalize()
        assert report.cloud_free is True
        assert len(report.events) == 0

    def test_private_network_ollama(self) -> None:
        """Ollama on a LAN server should count as local."""
        guard = NetworkGuard(run_id="test-lan")
        guard.record_llm_call(
            role="architect",
            provider="openai-compatible",
            base_url="http://192.168.1.50:11434/v1",
            model="qwen3-coder:30b",
        )
        report = guard.finalize()
        assert report.cloud_free is True

    def test_fingerprint_changes_with_different_runs(self) -> None:
        g1 = NetworkGuard(run_id="run-a")
        g1.record_llm_call(role="a", provider="mock")
        r1 = g1.finalize()

        g2 = NetworkGuard(run_id="run-b")
        g2.record_llm_call(role="a", provider="mock")
        r2 = g2.finalize()

        # Different run IDs should produce different fingerprints
        assert r1.fingerprint != r2.fingerprint

    def test_enforcement_mode_is_always_audit(self) -> None:
        guard = NetworkGuard(run_id="x")
        guard.record_llm_call(role="a", provider="openai-compatible", base_url="http://localhost")
        assert guard.finalize().summary["enforcement_mode"] == "audit"

    def test_report_is_a_snapshot(self) -> None:
        guard = NetworkGuard(run_id="x")
        report = guard.finalize()
        guard.record_sandbox_call("docker")
        assert report.events == []

    def test_for_config_reads_local_hosts_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LOCAL_HOSTS_ENV, "gpu.lab.example.com, git.lab.example.com")
        guard = NetworkGuard.for_config("x", config=None)
        guard.record_llm_call(
            role="a", provider="openai-compatible", base_url="http://gpu.lab.example.com:11434"
        )
        guard.record_external_call("git_push", "git.lab.example.com")
        assert guard.finalize().cloud_free is True

    def test_for_config_adds_the_configured_local_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from delegatron.config import Config, PipelineConfig

        monkeypatch.delenv(LOCAL_HOSTS_ENV, raising=False)
        config = Config(pipeline=PipelineConfig(local_hosts=("gpu.lab.example.com",)))
        guard = NetworkGuard.for_config("x", config=config)
        guard.record_llm_call(
            role="a", provider="openai-compatible", base_url="http://gpu.lab.example.com:11434"
        )
        assert guard.finalize().cloud_free is True

    @pytest.mark.parametrize("host", ["100.101.102.103", "gpu-box.lan", "fd12::1", "0.0.0.0"])
    def test_lan_hosts_count_like_model_endpoints(self, host: str) -> None:
        # One locality rule for cost, prompts and the zero-cloud report (D1).
        assert is_local_host(host) is True


class TestFingerprint:
    def _report(self) -> dict:
        guard = NetworkGuard(run_id="fp")
        guard.record_llm_call(
            role="a", provider="openai-compatible", base_url="http://ollama:11434"
        )
        guard.record_sandbox_call("docker")
        return guard.finalize().to_dict()

    def test_fingerprint_survives_a_json_round_trip(self) -> None:
        report = json.loads(json.dumps(self._report(), indent=2, default=str))
        assert report_fingerprint(report) == report["fingerprint"]

    @pytest.mark.parametrize("field", ["host", "provider", "is_local", "details", "category"])
    def test_any_event_change_changes_the_fingerprint(self, field: str) -> None:
        report = self._report()
        report["events"][0][field] = "tampered"
        assert report_fingerprint(report) != report["fingerprint"]

    def test_top_level_change_changes_the_fingerprint(self) -> None:
        report = self._report()
        report["cloud_free"] = not report["cloud_free"]
        assert report_fingerprint(report) != report["fingerprint"]


class TestCostTrackingLocalModels:
    """Test that local models are tracked as $0 cost."""

    def test_ollama_model_is_free(self) -> None:
        from delegatron.utils.cost import _is_local_model

        assert _is_local_model("qwen3-coder:30b")
        assert _is_local_model("deepseek-r1:14b-qwen-distill-q4_K_M")
        assert _is_local_model("llama3.2:8b")
        assert _is_local_model("mistral:7b")
        assert _is_local_model("codestral:22b")
        assert _is_local_model("phi-3:mini")

    def test_cloud_model_is_not_free(self) -> None:
        from delegatron.utils.cost import _is_local_model

        assert not _is_local_model("claude-sonnet-4-20250514")
        assert not _is_local_model("gpt-4.1")
        assert not _is_local_model("gemini-2.5-pro")

    def test_cost_tracker_zero_for_local(self) -> None:
        from delegatron.utils.cost import CostTracker

        tracker = CostTracker()
        cost = tracker.record(
            model="qwen3-coder:30b",
            input_tokens=1000,
            output_tokens=500,
            role="implementer",
        )
        assert cost == 0.0
        assert tracker.total_cost_usd == 0.0

    def test_cost_tracker_nonzero_for_cloud(self) -> None:
        from delegatron.utils.cost import CostTracker

        tracker = CostTracker()
        cost = tracker.record(
            model="claude-sonnet-4-20250514",
            input_tokens=1000,
            output_tokens=500,
            role="architect",
        )
        assert cost > 0.0
