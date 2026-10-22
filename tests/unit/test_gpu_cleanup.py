"""Tests for GPU memory cleanup utility."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from delegatron.config import Config, RoleConfig
from delegatron.utils.gpu_cleanup import (
    cleanup_after_run,
    get_ollama_models,
    unload_except,
    unload_ollama_models,
)


def _ollama_config() -> Config:
    return Config(
        architect=RoleConfig(
            provider="openai-compatible",
            model="qwen3.5:9b",
            base_url="http://localhost:11434/v1",
        ),
        implementer=RoleConfig(
            provider="openai-compatible",
            model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1",
        ),
        reviewer=RoleConfig(
            provider="openai-compatible",
            model="qwen3.5:9b",
            base_url="http://localhost:11434/v1",
        ),
    )


def _cloud_config() -> Config:
    return Config(
        architect=RoleConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
        implementer=RoleConfig(provider="openai", model="gpt-4.1"),
        reviewer=RoleConfig(provider="google", model="gemini-2.5-pro"),
    )


class TestGetOllamaModels:
    def test_extracts_ollama_models(self) -> None:
        models = get_ollama_models(_ollama_config())
        assert models == {"qwen3.5:9b", "qwen3-coder:30b"}

    def test_empty_for_cloud_config(self) -> None:
        models = get_ollama_models(_cloud_config())
        assert models == set()

    def test_mixed_config(self) -> None:
        config = Config(
            architect=RoleConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
            implementer=RoleConfig(
                provider="openai-compatible",
                model="qwen3-coder:30b",
                base_url="http://localhost:11434/v1",
            ),
            reviewer=RoleConfig(provider="google", model="gemini-2.5-pro"),
        )
        models = get_ollama_models(config)
        assert models == {"qwen3-coder:30b"}


class TestUnloadOllamaModels:
    @patch("delegatron.utils.gpu_cleanup.subprocess.run")
    def test_unloads_all_models(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        unloaded = unload_ollama_models(_ollama_config())
        assert len(unloaded) == 2
        assert mock_run.call_count == 2

    @patch("delegatron.utils.gpu_cleanup.subprocess.run")
    def test_handles_model_not_loaded(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="model not loaded")
        unloaded = unload_ollama_models(_ollama_config())
        assert len(unloaded) == 0

    @patch("delegatron.utils.gpu_cleanup.subprocess.run", side_effect=FileNotFoundError)
    def test_handles_ollama_not_installed(self, mock_run: MagicMock) -> None:
        unloaded = unload_ollama_models(_ollama_config())
        assert len(unloaded) == 0

    def test_skips_cloud_config(self) -> None:
        unloaded = unload_ollama_models(_cloud_config())
        assert unloaded == []


class TestUnloadExcept:
    @patch("delegatron.utils.gpu_cleanup.unload_model")
    def test_unloads_other_models_keeps_active(self, mock_unload: MagicMock) -> None:
        mock_unload.return_value = True
        unload_except(_ollama_config(), keep_model="qwen3-coder:30b")
        # Should unload qwen3.5:9b but NOT qwen3-coder:30b
        unloaded_models = [call.args[0] for call in mock_unload.call_args_list]
        assert "qwen3-coder:30b" not in unloaded_models
        assert "qwen3.5:9b" in unloaded_models

    @patch("delegatron.utils.gpu_cleanup.unload_model")
    def test_no_ops_for_cloud_config(self, mock_unload: MagicMock) -> None:
        unload_except(_cloud_config(), keep_model="gpt-4.1")
        mock_unload.assert_not_called()


class TestCleanupAfterRun:
    @patch("delegatron.utils.gpu_cleanup.unload_ollama_models", return_value=["qwen3.5:9b"])
    def test_calls_unload(self, mock_unload: MagicMock) -> None:
        cleanup_after_run(_ollama_config())
        mock_unload.assert_called_once()

    @patch("delegatron.utils.gpu_cleanup.unload_ollama_models", side_effect=RuntimeError("boom"))
    def test_never_raises(self, mock_unload: MagicMock) -> None:
        # Should not propagate exceptions
        cleanup_after_run(_ollama_config())
