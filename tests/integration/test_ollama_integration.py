"""Integration tests verified against the Ollama code path.

Tests the Ollama-specific code paths in isolation with mocked HTTP:
- Provider adapter (OpenAI-compatible with Ollama detection)
- Preset and config loading for all Ollama presets
- Autoselect heuristics
- Context gathering for multi-file implementer input
- JSON repair for local model output (think tags, code fences, prose)
- Streaming token delivery
- Error handling (connection, not found, OOM)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delegatron.agents.providers.base import ProviderResponse
from delegatron.autoselect import select_preset
from delegatron.config import PRESETS, load_config
from delegatron.context import gather_context

# ---------------------------------------------------------------------------
# Ollama preset loading
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOllamaPresetLoading:
    """Verify all Ollama presets load correctly with expected configuration."""

    OLLAMA_PRESETS = ("local", "local-phi4", "local-heavy")

    def test_all_ollama_presets_exist(self) -> None:
        for name in self.OLLAMA_PRESETS:
            assert name in PRESETS, f"Missing Ollama preset: {name}"

    @pytest.mark.parametrize("preset_name", OLLAMA_PRESETS)
    def test_ollama_preset_config_fields(self, preset_name: str) -> None:
        """Each Ollama preset should have correct provider and base_url."""
        config = load_config(preset=preset_name)

        for role_name in ("architect", "implementer", "reviewer"):
            role = getattr(config, role_name)
            assert role.provider == "openai-compatible", (
                f"{preset_name}.{role_name}: expected provider='openai-compatible', "
                f"got {role.provider!r}"
            )
            assert "localhost:11434" in role.base_url, (
                f"{preset_name}.{role_name}: expected Ollama base_url, got {role.base_url!r}"
            )
            # prompt_version 1 (cloud prompt) or 3-5 (local prompts) are all valid
            assert role.prompt_version in (1, 3, 4, 5), (
                f"{preset_name}.{role_name}: expected prompt_version in (1,3,4,5), "
                f"got {role.prompt_version}"
            )

    def test_local_uses_expected_models(self) -> None:
        config = load_config(preset="local")
        assert config.architect.model == "phi4:14b"
        assert config.implementer.model == "qwen2.5-coder:14b"
        assert config.reviewer.model == "phi4:14b"

    def test_local_phi4_uses_phi4(self) -> None:
        config = load_config(preset="local-phi4")
        assert config.architect.model == "phi4:14b"
        assert config.implementer.model == "phi4:14b"
        assert config.reviewer.model == "phi4:14b"

    def test_local_heavy_uses_qwen3_coder_30b(self) -> None:
        config = load_config(preset="local-heavy")
        assert config.architect.model == "phi4:14b"
        assert config.implementer.model == "qwen3-coder:30b"
        assert config.reviewer.model == "phi4:14b"


# ---------------------------------------------------------------------------
# Provider adapter — request building and Ollama detection
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOllamaProviderAdapter:
    """Test the OpenAI-compatible provider with Ollama-specific behaviour."""

    def _make_provider(self, base_url: str = "http://localhost:11434/v1"):
        from delegatron.agents.providers.openai_provider import OpenAIProvider

        with (
            patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
            patch("delegatron.agents.providers.openai_provider.openai", create=True),
        ):
            return OpenAIProvider(api_key="", model="qwen3-coder:30b", base_url=base_url)

    def test_ollama_detection_positive(self) -> None:
        provider = self._make_provider("http://localhost:11434/v1")
        assert provider._is_ollama is True

    def test_ollama_detection_negative(self) -> None:
        provider = self._make_provider("https://api.openai.com/v1")
        assert provider._is_ollama is False

    def test_build_request_appends_schema_to_prompt(self) -> None:
        """Ollama uses json_object format with schema appended to system prompt."""
        provider = self._make_provider()
        schema = {
            "title": "WorkPlan",
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        }
        kwargs = provider._build_request_kwargs(
            system_prompt="You are an architect.",
            user_message="Plan the task.",
            json_schema=schema,
            max_tokens=4096,
            temperature=0.0,
        )

        assert kwargs["response_format"]["type"] == "json_object"
        assert "WorkPlan" in kwargs["messages"][0]["content"]
        assert '"summary"' in kwargs["messages"][0]["content"]

    def test_build_request_without_schema(self) -> None:
        """Without a schema, no response_format should be set."""
        provider = self._make_provider()
        kwargs = provider._build_request_kwargs(
            system_prompt="Hello.",
            user_message="World.",
            json_schema=None,
            max_tokens=4096,
            temperature=0.0,
        )
        assert "response_format" not in kwargs


# ---------------------------------------------------------------------------
# Error handling — connection, not found, OOM
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOllamaErrorFlows:
    """Verify Ollama-specific error messages are actionable."""

    def _make_provider(self):
        from delegatron.agents.providers.openai_provider import OpenAIProvider

        with (
            patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
            patch("delegatron.agents.providers.openai_provider.openai", create=True),
        ):
            return OpenAIProvider(
                api_key="",
                model="qwen3-coder:30b",
                base_url="http://localhost:11434/v1",
            )

    def test_connection_error_suggests_ollama_serve(self) -> None:
        provider = self._make_provider()
        from delegatron.agents.providers.openai_provider import OllamaConnectionError

        err = provider._friendly_connection_error(ConnectionError("Connection refused"))
        assert isinstance(err, OllamaConnectionError)
        assert "ollama serve" in str(err)

    def test_not_found_error_suggests_ollama_pull(self) -> None:
        provider = self._make_provider()
        from delegatron.agents.providers.openai_provider import OllamaConnectionError

        err = provider._friendly_not_found_error(Exception("404 Not Found"))
        assert isinstance(err, OllamaConnectionError)
        assert "ollama pull qwen3-coder:30b" in str(err)

    def test_oom_error_suggests_smaller_model(self) -> None:
        provider = self._make_provider()
        from delegatron.agents.providers.openai_provider import OllamaConnectionError

        fake_exc = MagicMock()
        fake_exc.status_code = 500
        fake_exc.body = "CUDA error: out of memory allocating tensor"
        err = provider._friendly_status_error(fake_exc)
        assert isinstance(err, OllamaConnectionError)
        assert "local" in str(err) or "smaller model" in str(err).lower()

    def test_generic_500_suggests_repull(self) -> None:
        provider = self._make_provider()
        fake_exc = MagicMock()
        fake_exc.status_code = 500
        fake_exc.body = "internal server error"
        err = provider._friendly_status_error(fake_exc)
        assert "ollama rm" in str(err)
        assert "ollama pull" in str(err)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ollama_streaming_yields_tokens() -> None:
    """Mock an async stream and verify tokens are yielded then final response."""
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    with (
        patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
        patch("delegatron.agents.providers.openai_provider.openai") as mock_openai,
    ):
        # Build fake stream events
        events = []
        for i, token in enumerate(["Hello", " ", "world"]):
            event = MagicMock()
            event.id = "chatcmpl-stream" if i == 0 else None
            event.model = "qwen3-coder:30b" if i == 0 else None
            event.usage = None
            choice = MagicMock()
            choice.delta.content = token
            event.choices = [choice]
            events.append(event)

        # Final event with usage
        final_event = MagicMock()
        final_event.id = None
        final_event.model = None
        final_event.usage = MagicMock()
        final_event.usage.prompt_tokens = 100
        final_event.usage.completion_tokens = 3
        final_event.choices = []
        events.append(final_event)

        # Create async iterator
        async def fake_stream():
            for e in events:
                yield e

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
        mock_openai.AsyncOpenAI.return_value = mock_client

        provider = OpenAIProvider(
            api_key="",
            model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1",
        )

    # Collect stream output
    tokens: list[str] = []
    final_response: ProviderResponse | None = None

    async for item in provider.stream_generate(
        system_prompt="Test",
        user_message="Test",
    ):
        if isinstance(item, ProviderResponse):
            final_response = item
        else:
            tokens.append(item)

    assert tokens == ["Hello", " ", "world"]
    assert final_response is not None
    assert final_response.content == "Hello world"
    assert final_response.input_tokens == 100
    assert final_response.output_tokens == 3


# ---------------------------------------------------------------------------
# Autoselect heuristics
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAutoselect:
    """Verify preset auto-selection based on task and project."""

    def test_simple_task_selects_budget(self) -> None:
        preset, reason = select_preset(
            "fix a simple typo in the README",
            {"languages": ["python"], "frameworks": [], "file_count": 10},
        )
        assert preset == "budget"
        assert "simple" in reason.lower()

    def test_complex_task_selects_claude_heavy(self) -> None:
        # "refactor" (+2) + large project (+1) + hard language (+1) = 4 → claude-heavy
        preset, _reason = select_preset(
            "refactor the authentication system to use OAuth2",
            {"languages": ["rust"], "frameworks": [], "file_count": 80},
        )
        assert preset == "claude-heavy"

    def test_hard_language_favors_heavy(self) -> None:
        preset, _reason = select_preset(
            "add a new HTTP handler",
            {"languages": ["rust"], "frameworks": [], "file_count": 60},
        )
        assert preset == "claude-heavy"

    def test_balanced_task_selects_default(self) -> None:
        # No fast/heavy keywords, no hard lang, small project → score ~0 → default
        preset, _reason = select_preset(
            "add a new endpoint for users",
            {"languages": ["java"], "frameworks": [], "file_count": 20},
        )
        assert preset == "default"


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestContextGathering:
    """Verify gather_context selects relevant files for the implementer."""

    def test_finds_related_files(self, tmp_path: Path) -> None:
        """Gathers task output files, siblings, tests, and init files."""
        # Set up workspace
        (tmp_path / "src" / "app").mkdir(parents=True)
        (tmp_path / "src" / "app" / "__init__.py").write_text("")
        (tmp_path / "src" / "app" / "health.py").write_text("def health(): pass\n")
        (tmp_path / "src" / "app" / "main.py").write_text("app = None\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_health.py").write_text("def test_health(): pass\n")

        file_tree = [
            "src/app/__init__.py",
            "src/app/health.py",
            "src/app/main.py",
            "tests/test_health.py",
        ]

        task = {
            "task_id": "t1",
            "title": "Fix health endpoint",
            "description": "Update src/app/health.py",
            "outputs": ["src/app/health.py"],
        }

        result = gather_context(
            workspace_root=tmp_path,
            task=task,
            file_tree=file_tree,
        )

        # Should include the output file
        assert "src/app/health.py" in result
        # Should include __init__.py (same directory)
        assert "src/app/__init__.py" in result
        # Should include sibling file
        assert "src/app/main.py" in result

    def test_respects_size_limit(self, tmp_path: Path) -> None:
        """Total gathered context should stay under the size limit."""
        (tmp_path / "src").mkdir()
        # Create a large file (60KB)
        large_content = "x" * 60_000
        (tmp_path / "src" / "big.py").write_text(large_content)
        (tmp_path / "src" / "small.py").write_text("small = True\n")

        file_tree = ["src/big.py", "src/small.py"]
        task = {
            "task_id": "t1",
            "title": "Update",
            "description": "Update src/big.py",
            "outputs": ["src/big.py"],
        }

        result = gather_context(
            workspace_root=tmp_path,
            task=task,
            file_tree=file_tree,
            max_total_size=50_000,
        )

        total_size = sum(len(v.encode()) for v in result.values())
        assert total_size <= 50_000 + 100  # small margin for truncation marker


# ---------------------------------------------------------------------------
# JSON repair (local model output)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestJsonRepair:
    """Verify the agent base class repairs malformed local model output."""

    def _parse(self, content: str) -> dict[str, Any]:
        """Helper: parse content through BaseAgent._parse_response."""
        from delegatron.agents.base import BaseAgent

        # Create a minimal concrete subclass for testing
        class DummyAgent(BaseAgent):
            def _build_user_message(self, input_data):
                return ""

            def _get_output_schema(self):
                return {}

            def _get_output_model(self):
                from pydantic import BaseModel

                class Simple(BaseModel):
                    name: str
                    value: int

                return Simple

        agent = DummyAgent(provider=MagicMock(), system_prompt="test")
        response = ProviderResponse(content=content, model="test")
        return agent._parse_response(response, agent._get_output_model())

    def test_strips_think_tags(self) -> None:
        content = '<think>Let me reason about this...</think>{"name": "test", "value": 42}'
        result = self._parse(content)
        assert result["name"] == "test"
        assert result["value"] == 42

    def test_strips_code_fences(self) -> None:
        content = '```json\n{"name": "fenced", "value": 1}\n```'
        result = self._parse(content)
        assert result["name"] == "fenced"
        assert result["value"] == 1

    def test_extracts_json_from_prose(self) -> None:
        content = 'Here is the result:\n{"name": "embedded", "value": 99}\nDone!'
        result = self._parse(content)
        assert result["name"] == "embedded"
        assert result["value"] == 99

    def test_handles_multiline_think_tags(self) -> None:
        content = (
            "<think>\nStep 1: Consider the task\n"
            "Step 2: Plan the approach\n</think>\n"
            '{"name": "multi", "value": 7}'
        )
        result = self._parse(content)
        assert result["name"] == "multi"

    def test_clean_json_passes_through(self) -> None:
        content = '{"name": "clean", "value": 0}'
        result = self._parse(content)
        assert result["name"] == "clean"
        assert result["value"] == 0
