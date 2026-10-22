"""Tests for provider adapters and registry routing."""

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from delegatron.agents.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    FINISH_REASON_LENGTH,
    ProviderError,
    ProviderRequestError,
    ProviderResponse,
    ProviderTimeoutError,
    ProviderTransientError,
    error_for_status,
    retry_after_seconds,
    stream_error,
    token_count,
)
from delegatron.agents.providers.openai_provider import NO_API_KEY
from delegatron.agents.providers.registry import get_provider
from delegatron.config import RoleConfig
from delegatron.schemas.patch_set import PatchSet
from delegatron.schemas.review_report import ReviewReport
from delegatron.schemas.work_plan import WorkPlan
from delegatron.utils.retry import retry_with_backoff

SCHEMAS = pytest.mark.parametrize(
    "model_cls", [PatchSet, WorkPlan, ReviewReport], ids=lambda m: m.__name__
)


def test_provider_response_creation():
    resp = ProviderResponse(
        content='{"key": "value"}',
        model="test-model",
        input_tokens=100,
        output_tokens=50,
    )
    assert resp.content == '{"key": "value"}'
    assert resp.model == "test-model"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    assert (resp.cache_read_tokens, resp.cache_write_tokens) == (0, 0)


def test_token_count_tolerates_missing_and_null_counters():
    from types import SimpleNamespace

    usage = SimpleNamespace(prompt_tokens=12, cached_tokens=None)
    assert token_count(usage, "prompt_tokens") == 12
    assert token_count(usage, "cached_tokens") == 0
    assert token_count(usage, "absent") == 0
    assert token_count(None, "prompt_tokens") == 0
    assert token_count(MagicMock(), "prompt_tokens") == 0


def test_default_request_timeout_matches_the_sdk_default():
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    assert DEFAULT_TIMEOUT_SECONDS == 600
    assert OpenAIProvider(api_key="k", model="gpt-4.1")._client.timeout.read == 600


def _pyproject_requirements(*extras: str) -> list[Any]:
    """Requirements of the project's base dependencies plus *extras*."""
    import tomllib
    from pathlib import Path

    from packaging.requirements import Requirement

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    specs = [*project["dependencies"]]
    for extra in extras:
        specs += project["optional-dependencies"][extra]
    return [Requirement(spec) for spec in specs]


def test_google_genai_floor_has_response_json_schema():
    """The adapter sends ``response_json_schema`` (#90); older SDKs lack the field."""
    (genai,) = [r for r in _pyproject_requirements("cloud-providers") if r.name == "google-genai"]
    assert genai.specifier.contains("1.22.0")
    assert not genai.specifier.contains("1.21.1")

    types = pytest.importorskip("google.genai.types")
    assert "response_json_schema" in types.GenerateContentConfig.model_fields


def test_installed_packages_satisfy_pyproject():
    """What the suite runs against meets every floor of ``.[dev,cloud-providers]``."""
    from importlib import metadata

    unmet = []
    for requirement in _pyproject_requirements("dev", "cloud-providers"):
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            continue  # an optional SDK that is not installed here
        if not requirement.specifier.contains(installed, prereleases=True):
            unmet.append(f"{requirement} (installed {installed})")
    assert unmet == []


def test_registry_anthropic():
    """Anthropic routes to AnthropicProvider."""
    config = RoleConfig(provider="anthropic", model="claude-sonnet-4-20250514", api_key="test")
    try:
        provider = get_provider(config)
        from delegatron.agents.providers.anthropic import AnthropicProvider

        assert isinstance(provider, AnthropicProvider)
    except RuntimeError as e:
        # Anthropic SDK not installed — that's expected in some test envs
        assert "Anthropic SDK not installed" in str(e)


@patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True)
@patch("delegatron.agents.providers.openai_provider.openai", create=True)
def test_registry_openai(mock_openai):
    """OpenAI routes to OpenAIProvider (it's OpenAI-compatible)."""
    config = RoleConfig(provider="openai", model="gpt-4.1", api_key="test")
    provider = get_provider(config)
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    assert isinstance(provider, OpenAIProvider)


@patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True)
@patch("delegatron.agents.providers.openai_provider.openai", create=True)
def test_registry_openai_compatible(mock_openai):
    config = RoleConfig(
        provider="openai-compatible",
        model="llama4",
        api_key="test",
        base_url="http://localhost:11434/v1",
    )
    provider = get_provider(config)
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    assert isinstance(provider, OpenAIProvider)


def test_registry_google():
    """Google routes to GoogleProvider."""
    config = RoleConfig(provider="google", model="gemini-2.5-pro", api_key="test")
    try:
        provider = get_provider(config)
        from delegatron.agents.providers.google import GoogleProvider

        assert isinstance(provider, GoogleProvider)
    except (RuntimeError, NameError):
        # Google SDK not installed or _HAS_GOOGLE not defined — expected
        pass


@patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True)
@patch("delegatron.agents.providers.openai_provider.openai", create=True)
def test_registry_unknown_defaults_to_openai(mock_openai):
    config = RoleConfig(provider="some-future-provider", model="test", api_key="test")
    provider = get_provider(config)
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    assert isinstance(provider, OpenAIProvider)


def test_registry_mock():
    config = RoleConfig(provider="mock", model="mock-architect")
    provider = get_provider(config)
    from delegatron.agents.providers.mock import MockProvider

    assert isinstance(provider, MockProvider)


# --- Ollama-specific error handling tests ---


class TestOllamaErrorMessages:
    """Test that Ollama errors produce friendly, actionable messages."""

    def _make_ollama_provider(self):
        """Create an OpenAI provider configured like Ollama."""
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

    def test_connection_error_mentions_ollama_serve(self):
        provider = self._make_ollama_provider()
        err = provider._friendly_connection_error(ConnectionError("refused"))
        assert "ollama serve" in str(err)
        assert "localhost:11434" in str(err)

    def test_not_found_error_mentions_ollama_pull(self):
        provider = self._make_ollama_provider()
        err = provider._friendly_not_found_error(Exception("404"))
        assert "ollama pull" in str(err)
        assert "qwen3-coder:30b" in str(err)
        assert "ollama list" in str(err)

    def test_oom_error_suggests_smaller_model(self):
        provider = self._make_ollama_provider()
        fake_exc = MagicMock()
        fake_exc.status_code = 500
        fake_exc.body = "out of memory allocating tensor"
        err = provider._friendly_status_error(fake_exc)
        assert "out of memory" in str(err).lower()
        assert "smaller model" in str(err).lower() or "local" in str(err)

    def test_non_ollama_connection_error_is_generic(self):
        """Non-Ollama providers get generic error messages."""
        from delegatron.agents.providers.openai_provider import OpenAIProvider

        with (
            patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
            patch("delegatron.agents.providers.openai_provider.openai", create=True),
        ):
            provider = OpenAIProvider(api_key="test", model="gpt-4.1", base_url="")
        err = provider._friendly_connection_error(ConnectionError("refused"))
        assert "ollama" not in str(err).lower()
        assert "openai" in str(err).lower()

    def test_is_ollama_detection(self):
        provider = self._make_ollama_provider()
        assert provider._is_ollama is True

        from delegatron.agents.providers.openai_provider import OpenAIProvider

        with (
            patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
            patch("delegatron.agents.providers.openai_provider.openai", create=True),
        ):
            non_ollama = OpenAIProvider(api_key="k", model="m", base_url="")
        assert non_ollama._is_ollama is False


# --- Context-window detection (OpenAI-compatible local endpoints) ---


_OLLAMA_URL = "http://localhost:11434/v1"
_OLLAMA_MODEL = "qwen2.5-coder:14b"
_OLLAMA_VERSION = {"version": "0.12.6"}


def _ollama_provider(base_url: str = _OLLAMA_URL, model: str = _OLLAMA_MODEL) -> Any:
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(api_key="ollama", model=model, base_url=base_url, timeout_seconds=30)


def _ps(*models: tuple[str, int]) -> dict[str, Any]:
    """An ``/api/ps`` reply listing *models* as ``(name, context_length)``."""
    return {"models": [{"name": n, "model": n, "context_length": c} for n, c in models]}


def _serve(provider: Any, routes: dict[str, Any]) -> list[httpx.Request]:
    """Answer *provider*'s requests by URL path from *routes* (real SDK, mock transport).

    A route is a JSON body, an ``httpx.Response``, an exception to raise, or a
    callable returning one of those. Unknown paths get a 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(request.url.path)
        if callable(route):
            route = route(request)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(route, Exception):
            raise route
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    return _with_transport(provider, handler)


def _paths(requests: list[httpx.Request]) -> list[str]:
    return [request.url.path for request in requests]


@pytest.fixture
def provider_log(caplog):
    """Capture the OpenAI adapter's log even when another test turned off propagation."""
    logger = logging.getLogger("delegatron.agents.providers.openai_provider")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=logger.name)
    yield caplog
    logger.removeHandler(caplog.handler)


class TestDetectContextWindow:
    """OpenAIProvider.detect_context_window — never touches a real server."""

    @pytest.fixture(autouse=True)
    def _no_env_context_length(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)

    async def test_reads_modelfile_num_ctx(self):
        provider = _ollama_provider()
        requests = _serve(
            provider,
            {
                "/api/version": _OLLAMA_VERSION,
                "/api/show": {
                    "parameters": 'num_ctx                        16384\nstop    "<|im_end|>"',
                    "model_info": {"qwen2.context_length": 32768},
                },
            },
        )

        assert await provider.detect_context_window() == 16384
        assert _paths(requests) == ["/api/version", "/api/show"]
        assert requests[0].method == "GET"
        assert str(requests[1].url) == "http://localhost:11434/api/show"
        assert requests[1].method == "POST"
        assert json.loads(requests[1].content) == {"model": _OLLAMA_MODEL}

    async def test_probe_uses_no_retries_and_a_short_timeout(self):
        provider = _ollama_provider()
        provider._client = MagicMock()
        native = provider._client.with_options.return_value
        native.get = AsyncMock(return_value=httpx.Response(200, json=_OLLAMA_VERSION))
        native.post = AsyncMock(
            return_value=httpx.Response(200, json={"parameters": "num_ctx 8192"})
        )

        assert await provider.detect_context_window() == 8192
        options = provider._client.with_options.call_args.kwargs
        assert options["max_retries"] == 0
        assert options["timeout"] <= 5

    async def test_capped_by_trained_context_length(self):
        provider = _ollama_provider()
        _serve(
            provider,
            {
                "/api/version": _OLLAMA_VERSION,
                "/api/show": {
                    "parameters": "num_ctx 65536",
                    "model_info": {"phi3.context_length": 16384},
                },
            },
        )
        assert await provider.detect_context_window() == 16384

    async def test_runtime_context_length_of_the_loaded_model(self, monkeypatch):
        """``/api/ps`` beats the local environment: it is what the server really runs."""
        monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "8192")
        provider = _ollama_provider()
        _serve(
            provider,
            {
                "/api/version": _OLLAMA_VERSION,
                "/api/show": {"model_info": {"qwen2.context_length": 131072}},
                "/api/ps": _ps(("phi4:14b", 2048), (_OLLAMA_MODEL, 32768)),
            },
        )
        assert await provider.detect_context_window() == 32768

    async def test_loaded_model_matches_the_implicit_latest_tag(self):
        provider = _ollama_provider(model="llama3")
        _serve(
            provider,
            {
                "/api/version": _OLLAMA_VERSION,
                "/api/show": {},
                "/api/ps": _ps(("llama3:latest", 6144)),
            },
        )
        assert await provider.detect_context_window() == 6144

    async def test_env_used_when_the_model_is_not_loaded(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "8192")
        provider = _ollama_provider()
        _serve(
            provider,
            {
                "/api/version": _OLLAMA_VERSION,
                "/api/show": {"parameters": 'stop "<|im_end|>"'},
                "/api/ps": _ps(("phi4:14b", 16384)),
            },
        )
        assert await provider.detect_context_window() == 8192

    async def test_defaults_to_ollama_runtime_default(self):
        provider = _ollama_provider()
        _serve(
            provider,
            {
                "/api/version": _OLLAMA_VERSION,
                "/api/show": {"model_info": {"llama.context_length": 131072}},
                "/api/ps": {"models": []},
            },
        )
        assert await provider.detect_context_window() == 4096

    async def test_model_info_failure_on_ollama_falls_back_to_default(self, monkeypatch):
        provider = _ollama_provider()
        _serve(provider, {"/api/version": _OLLAMA_VERSION, "/api/show": httpx.Response(500)})
        assert await provider.detect_context_window() == 4096

        monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "16384")
        provider = _ollama_provider()
        _serve(provider, {"/api/version": _OLLAMA_VERSION})
        assert await provider.detect_context_window() == 16384

    async def test_invalid_env_is_ignored(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "lots")
        provider = _ollama_provider()
        _serve(provider, {"/api/version": _OLLAMA_VERSION, "/api/show": {}})
        assert await provider.detect_context_window() == 4096

    @pytest.mark.parametrize(
        ("base_url", "version_route"),
        [
            ("http://localhost:1234/v1", None),  # LM Studio: 404
            ("http://127.0.0.1:8000/v1", {"detail": "Not Found"}),  # vLLM-style JSON
            ("http://localhost:4000", httpx.Response(200, text="<html>LiteLLM</html>")),
            ("http://localhost:8080/v1", httpx.Response(200, json=["not", "ollama"])),
        ],
        ids=["404", "json_without_version", "html", "json_list"],
    )
    async def test_non_ollama_local_server_is_unknown(
        self, base_url, version_route, monkeypatch, provider_log
    ):
        """LM Studio, vLLM, llama.cpp or a LiteLLM proxy is not given Ollama's 4096 default."""
        monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "8192")
        provider = _ollama_provider(base_url, model="gpt-4.1")
        requests = _serve(
            provider, {"/api/version": version_route, "/api/show": {"parameters": "num_ctx 1"}}
        )

        assert await provider.detect_context_window() == 0
        assert await provider.detect_context_window() == 0
        assert _paths(requests) == ["/api/version"]  # asked once, then cached
        assert "not an Ollama server" in provider_log.text
        assert "context_window" in provider_log.text

    @pytest.mark.parametrize(
        "base_url",
        ["http://192.168.1.20:11434/v1", "http://ollama:11434/v1", "http://gpu-box.lan:11434"],
    )
    async def test_ollama_on_the_network_is_sized(self, base_url):
        """A LAN or compose-service Ollama host gets the same guard as localhost (#75)."""
        provider = _ollama_provider(base_url)
        requests = _serve(
            provider, {"/api/version": _OLLAMA_VERSION, "/api/show": {"parameters": "num_ctx 8192"}}
        )
        assert await provider.detect_context_window() == 8192
        assert _paths(requests) == ["/api/version", "/api/show"]

    async def test_unreachable_server_is_asked_again(self):
        provider = _ollama_provider()
        up = False

        def version(request: httpx.Request) -> Any:
            return _OLLAMA_VERSION if up else httpx.ConnectError("refused", request=request)

        requests = _serve(
            provider, {"/api/version": version, "/api/show": {"parameters": "num_ctx 8192"}}
        )

        assert await provider.detect_context_window() == 0
        up = True
        assert await provider.detect_context_window() == 8192
        assert _paths(requests) == ["/api/version", "/api/version", "/api/show"]

    @pytest.mark.parametrize("status", [502, 503, 429])
    async def test_transient_status_is_asked_again(self, status):
        """A proxy in front of a stopped Ollama (502) is not cached as "not Ollama"."""
        provider = _ollama_provider()
        replies = [httpx.Response(status, json={"error": "upstream"})]

        def version(request: httpx.Request) -> Any:
            return replies.pop() if replies else _OLLAMA_VERSION

        requests = _serve(
            provider, {"/api/version": version, "/api/show": {"parameters": "num_ctx 8192"}}
        )

        assert await provider.detect_context_window() == 0
        assert await provider.detect_context_window() == 8192
        assert _paths(requests) == ["/api/version", "/api/version", "/api/show"]

    @pytest.mark.parametrize(("local", "probed"), [(True, True), (False, False)])
    async def test_unmarked_endpoint_is_probed_when_local_or_private(self, local, probed):
        """Without an Ollama marker, ``is_local_base_url`` decides (D1: LAN hosts are local)."""
        provider = _ollama_provider("http://10.0.0.5:8080/v1")
        requests = _serve(
            provider, {"/api/version": _OLLAMA_VERSION, "/api/show": {"parameters": "num_ctx 8192"}}
        )
        target = "delegatron.agents.providers.openai_provider.is_local_base_url"
        with patch(target, return_value=local) as is_local:
            window = await provider.detect_context_window()

        is_local.assert_called_once_with("http://10.0.0.5:8080/v1")
        assert window == (8192 if probed else 0)
        assert bool(requests) is probed

    async def test_result_is_cached(self):
        provider = _ollama_provider()
        requests = _serve(
            provider, {"/api/version": _OLLAMA_VERSION, "/api/show": {"parameters": "num_ctx 8192"}}
        )
        assert await provider.detect_context_window() == 8192
        assert await provider.detect_context_window() == 8192
        assert len(requests) == 2

    async def test_base_url_without_v1_suffix(self):
        provider = _ollama_provider("http://127.0.0.1:11434/")
        requests = _serve(provider, {"/api/version": _OLLAMA_VERSION, "/api/show": {}})
        await provider.detect_context_window()
        assert str(requests[0].url) == "http://127.0.0.1:11434/api/version"
        assert str(requests[1].url) == "http://127.0.0.1:11434/api/show"

    @pytest.mark.parametrize(
        "base_url", ["", "https://api.openai.com/v1", "https://api.groq.com/openai/v1"]
    )
    async def test_cloud_endpoint_is_unknown_without_a_request(self, base_url):
        provider = _ollama_provider(base_url)
        requests = _serve(provider, {"/api/version": _OLLAMA_VERSION})
        assert await provider.detect_context_window() == 0
        assert requests == []

    def test_not_detected_at_construction(self):
        assert _ollama_provider()._context_window is None


class TestContextWindowRecheck:
    """A window Ollama did not pin is re-read from /api/ps once the model is loaded."""

    @pytest.fixture(autouse=True)
    def _no_env_context_length(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)

    def _routes(self, loaded: list[tuple[str, int]], show: dict[str, Any]) -> dict[str, Any]:
        return {
            "/api/version": _OLLAMA_VERSION,
            "/api/show": show,
            "/api/ps": lambda request: _ps(*loaded),
            "/v1/chat/completions": lambda request: _openai_completion("{}", "stop"),
        }

    async def test_generate_rereads_the_loaded_window_once(self):
        provider = _ollama_provider()
        loaded: list[tuple[str, int]] = []
        requests = _serve(provider, self._routes(loaded, {}))

        assert await provider.detect_context_window() == 4096  # not loaded yet
        loaded.append((_OLLAMA_MODEL, 32768))
        await provider.generate("sys", "msg")
        assert await provider.detect_context_window() == 32768

        await provider.generate("sys", "msg")
        assert _paths(requests).count("/api/ps") == 2  # detection + one re-read

    async def test_stream_rereads_the_loaded_window(self):
        provider = _ollama_provider()
        loaded: list[tuple[str, int]] = []
        routes = self._routes(loaded, {"model_info": {"qwen2.context_length": 16384}})
        routes["/v1/chat/completions"] = lambda request: _sse_response(
            [_openai_chunk("{}", "stop"), "data: [DONE]\n\n"]
        )
        _serve(provider, routes)

        assert await provider.detect_context_window() == 4096
        loaded.append((_OLLAMA_MODEL, 65536))
        _ = [item async for item in provider.stream_generate("sys", "msg")]
        assert await provider.detect_context_window() == 16384  # capped by the trained length

    async def test_modelfile_window_is_not_rechecked(self):
        provider = _ollama_provider()
        requests = _serve(provider, self._routes([], {"parameters": "num_ctx 8192"}))

        assert await provider.detect_context_window() == 8192
        await provider.generate("sys", "msg")
        assert "/api/ps" not in _paths(requests)

    async def test_undetected_provider_never_probes(self):
        provider = _ollama_provider()
        requests = _serve(provider, self._routes([], {}))
        await provider.generate("sys", "msg")
        assert _paths(requests) == ["/v1/chat/completions"]


# --- Mock provider ---


async def test_mock_provider_arbitration_report_beats_architect_role():
    """The director runs on the architect's provider but must get an ArbitrationReport."""
    from delegatron.agents.providers.mock import MockProvider

    provider = MockProvider(role="architect")
    response = await provider.generate("sys", "msg", json_schema={"title": "ArbitrationReport"})
    assert json.loads(response.content) == {"resolutions": []}


class TestMockProviderTruncation:
    """Scripted ``finish_reason == "length"`` responses (#107)."""

    async def test_scripted_calls_are_cut_off(self):
        from delegatron.agents.providers.mock import MockProvider

        provider = MockProvider(role="implementer", truncate_calls={1})
        cut = await provider.generate("sys", "msg", {"title": "PatchSet"}, max_tokens=2048)
        full = await provider.generate("sys", "msg", {"title": "PatchSet"}, max_tokens=2048)

        assert cut.finish_reason == FINISH_REASON_LENGTH
        assert cut.output_tokens == 2048
        with pytest.raises(json.JSONDecodeError):
            json.loads(cut.content)
        assert full.finish_reason == "stop"
        assert full.content.startswith(cut.content)
        assert json.loads(full.content)["status"] == "ok"

    def test_no_truncation_by_default(self):
        from delegatron.agents.providers.mock import MockProvider

        assert MockProvider(role="reviewer")._truncate_calls == frozenset()

    async def test_reviewer_cut_off_twice_raises_truncated_output_error(self):
        from delegatron.agents.base import TruncatedOutputError
        from delegatron.agents.providers.mock import MockProvider
        from delegatron.agents.reviewer import ReviewerAgent

        provider = MockProvider(role="reviewer", truncate_calls={1, 2})
        agent = ReviewerAgent(provider=provider, system_prompt="s", max_tokens=1000)

        with pytest.raises(TruncatedOutputError) as exc_info:
            await agent.invoke({"request": "r", "diff": "+x"})
        assert isinstance(exc_info.value, ValueError)
        assert provider._call_count == 2
        assert exc_info.value.max_tokens == 2000  # the retry doubled the budget

    async def test_reviewer_recovers_from_one_cut_off(self):
        from delegatron.agents.providers.mock import MockProvider
        from delegatron.agents.reviewer import ReviewerAgent

        provider = MockProvider(role="reviewer", truncate_calls={1})
        agent = ReviewerAgent(provider=provider, system_prompt="s", max_tokens=1000)

        result = await agent.invoke({"request": "r", "diff": "+x"})

        assert provider._call_count == 2
        assert result["acceptance_met"] is True
        assert result["_meta"]["input_tokens"] == 1000
        assert result["_meta"]["output_tokens"] == 1000 + 300


# --- Shared provider-error helpers ---


class TestErrorClassification:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
    def test_client_errors_are_not_retryable(self, status):
        err = error_for_status(status, "rejected", {"retry-after": "5"})
        assert type(err) is ProviderRequestError
        assert err.retryable is False
        assert err.retry_after is None

    @pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 529])
    def test_throttling_and_server_errors_are_transient(self, status):
        err = error_for_status(status, "try later")
        assert type(err) is ProviderTransientError
        assert err.retryable is True
        assert isinstance(err, RuntimeError)

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"retry-after": "7"}, 7.0),
            ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
            ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
            ({"retry-after": "-3"}, 0.0),
            ({}, None),
            (None, None),
        ],
    )
    def test_retry_after_seconds(self, headers, expected):
        assert retry_after_seconds(headers) == expected

    def test_transient_status_carries_retry_after(self):
        assert error_for_status(429, "slow down", {"retry-after": "12"}).retry_after == 12.0

    def test_timeout_error_names_model_and_timeout(self):
        err = ProviderTimeoutError("qwen3-coder:30b", 900)
        assert "qwen3-coder:30b" in str(err)
        assert "900s" in str(err)
        assert "timeout_seconds" in str(err)
        assert err.retryable is True

    @pytest.mark.parametrize(
        "error", [httpx.ReadTimeout("slow"), TimeoutError("slow")], ids=["httpx", "builtin"]
    )
    def test_stream_timeout_is_a_timeout(self, error):
        assert isinstance(stream_error("m", 30, error), ProviderTimeoutError)

    def test_stream_breakage_is_transient(self):
        err = stream_error("m", 30, httpx.RemoteProtocolError("peer closed connection"))
        assert type(err) is ProviderTransientError
        assert "peer closed connection" in str(err)


# --- Client construction: one retry layer, the role's timeout ---


def test_openai_client_has_no_sdk_retries_and_role_timeout():
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    provider = OpenAIProvider(api_key="k", model="gpt-4.1", timeout_seconds=42)
    assert provider._client.max_retries == 0
    assert provider._client.timeout.read == 42
    assert provider._client.timeout.connect == 10


def test_anthropic_client_has_no_sdk_retries_and_role_timeout():
    pytest.importorskip("anthropic")
    from delegatron.agents.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key="k", model="claude-sonnet-4-20250514", timeout_seconds=42)
    assert provider._client.max_retries == 0
    assert provider._client.timeout.read == 42
    assert provider._client.timeout.connect == 10


def test_google_client_uses_role_timeout_without_retries():
    pytest.importorskip("google.genai")
    from delegatron.agents.providers.google import GoogleProvider

    provider = GoogleProvider(api_key="k", model="gemini-2.5-pro", timeout_seconds=42)
    http_options = provider._client._api_client._http_options
    assert http_options.timeout == 42_000  # milliseconds
    assert http_options.retry_options is None  # the SDK's "never retry" default


@pytest.mark.parametrize(
    ("provider", "module", "cls_name"),
    [
        ("openai", "openai_provider", "OpenAIProvider"),
        ("openai-compatible", "openai_provider", "OpenAIProvider"),
        ("anthropic", "anthropic", "AnthropicProvider"),
        ("google", "google", "GoogleProvider"),
    ],
)
def test_registry_passes_role_timeout(provider, module, cls_name):
    target = f"delegatron.agents.providers.{module}.{cls_name}"
    config = RoleConfig(provider=provider, model="m", api_key="k", timeout_seconds=77)
    with patch(target) as adapter:
        get_provider(config)
    assert adapter.call_args.kwargs["timeout_seconds"] == 77


# --- Registry: the API key comes only from RoleConfig.resolve_api_key() ---


class TestRegistryApiKeys:
    @pytest.fixture(autouse=True)
    def _env_keys(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        monkeypatch.setenv("FAKE_SECRET", "s3cret")

    @pytest.mark.parametrize(
        ("provider", "base_url"),
        [
            ("openai-compatible", "http://localhost:11434/v1"),
            ("openai-compatible", "http://attacker.example:9000/v1"),
            ("openai", "http://attacker.example:9000/v1"),
            ("anthropic", "http://attacker.example:9000"),
        ],
    )
    def test_foreign_base_url_gets_no_default_key(self, provider, base_url):
        if provider == "anthropic":
            pytest.importorskip("anthropic")
        client = get_provider(RoleConfig(provider=provider, model="m", base_url=base_url))._client
        assert client.api_key in ("", NO_API_KEY)
        assert client.api_key not in ("sk-real-openai", "sk-ant-real")

    def test_home_host_gets_default_key(self):
        config = RoleConfig(
            provider="openai", model="gpt-4.1", base_url="https://api.openai.com/v1"
        )
        assert get_provider(config)._client.api_key == "sk-real-openai"

    def test_explicit_api_key_env_is_honoured(self):
        config = RoleConfig(
            provider="openai-compatible",
            model="m",
            base_url="http://gpu-box:11434/v1",
            api_key_env="FAKE_SECRET",
        )
        assert get_provider(config)._client.api_key == "s3cret"

    def test_env_keys_withheld_when_not_allowed(self):
        config = RoleConfig(
            provider="openai-compatible",
            model="m",
            base_url="http://attacker.example:9000/v1",
            api_key_env="FAKE_SECRET",
            allow_env_key=False,
        )
        assert get_provider(config)._client.api_key == NO_API_KEY

    def test_anthropic_auth_token_follows_the_env_key_policy(self, monkeypatch):
        """The SDK reads ANTHROPIC_AUTH_TOKEN itself; a role barred from env keys drops it."""
        pytest.importorskip("anthropic")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-secret")
        role = RoleConfig(provider="anthropic", model="m", base_url="http://attacker.example")

        barred = get_provider(RoleConfig(**{**vars(role), "allow_env_key": False}))._client
        allowed = get_provider(role)._client

        assert barred.auth_token is None
        assert "Authorization" not in barred.auth_headers
        assert allowed.auth_token == "bearer-secret"


# --- OpenAI adapter: structured output request ---


def _real_openai_provider(base_url: str = "", model: str = "gpt-4.1", timeout: float = 30):
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(api_key="k", model=model, base_url=base_url, timeout_seconds=timeout)


class TestOpenAIStructuredOutput:
    @SCHEMAS
    @pytest.mark.parametrize("base_url", ["", "https://api.openai.com/v1"])
    def test_cloud_uses_non_strict_json_schema(self, model_cls, base_url):
        schema = model_cls.model_json_schema()
        kwargs = _real_openai_provider(base_url)._build_request_kwargs(
            "sys", "msg", schema, 4096, 0.0
        )
        response_format = kwargs["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is False
        assert response_format["json_schema"]["schema"] == schema
        assert kwargs["messages"][0]["content"] == "sys"

    @SCHEMAS
    def test_compatible_endpoint_appends_schema_to_prompt(self, model_cls):
        schema = model_cls.model_json_schema()
        kwargs = _real_openai_provider("http://localhost:11434/v1")._build_request_kwargs(
            "sys", "msg", schema, 4096, 0.0
        )
        assert kwargs["response_format"] == {"type": "json_object"}
        system = kwargs["messages"][0]["content"]
        assert system.startswith("sys\n\nYou MUST respond with valid JSON")
        assert json.dumps(schema, indent=2) in system


# --- OpenAI adapter: error mapping and finish_reason (real SDK, mock transport) ---


def _with_transport(provider: Any, handler: Callable[[httpx.Request], httpx.Response]) -> list:
    """Route *provider*'s SDK client through *handler*; returns the recorded requests."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    provider._client = provider._client.copy(http_client=httpx.AsyncClient(transport=transport))
    return requests


def _raise(error_cls: type[httpx.TransportError]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_cls("boom", request=request)

    return handler


def _status(code: int, body: Any = None, headers: dict[str, str] | None = None):
    return lambda request: httpx.Response(
        code, json=body or {"error": {"message": "nope"}}, headers=headers
    )


def _sse_response(events: list[str], error: Exception | None = None) -> httpx.Response:
    """A ``text/event-stream`` response that sends *events*, then fails with *error*."""

    async def body() -> AsyncIterator[bytes]:
        for event in events:
            yield event.encode()
        if error is not None:
            raise error

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def _openai_chunk(content: str = "", finish_reason: str | None = None) -> str:
    delta = {"content": content} if content else {}
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4.1",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


_OPENAI_USAGE = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}

# Usage as api.openai.com reports a prompt that hit the prompt cache.
_OPENAI_CACHED_USAGE = {
    "prompt_tokens": 2006,
    "completion_tokens": 300,
    "total_tokens": 2306,
    "prompt_tokens_details": {"cached_tokens": 768, "audio_tokens": 0},
}


def _openai_usage_chunk(usage: dict[str, Any]) -> str:
    """The final ``stream_options.include_usage`` chunk: no choices, only usage."""
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4.1",
        "choices": [],
        "usage": usage,
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _openai_completion(
    content: str, finish_reason: str, usage: dict[str, Any] | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4.1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage or _OPENAI_USAGE,
        },
    )


@pytest.fixture
def no_sleep():
    with patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
        yield sleep


class TestOpenAIErrors:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_client_errors_fail_after_one_request(self, status, no_sleep):
        provider = _real_openai_provider()
        requests = _with_transport(provider, _status(status))

        with pytest.raises(ProviderError) as exc_info:
            await retry_with_backoff(provider.generate, "sys", "msg")

        assert exc_info.value.retryable is False
        assert len(requests) == 1
        no_sleep.assert_not_awaited()

    @pytest.mark.parametrize(
        "handler", [_status(500), _raise(httpx.ConnectError)], ids=["http_500", "connect"]
    )
    async def test_transient_errors_get_four_attempts_and_no_sdk_retries(self, handler, no_sleep):
        provider = _real_openai_provider()
        requests = _with_transport(provider, handler)

        with pytest.raises(ProviderTransientError):
            await retry_with_backoff(provider.generate, "sys", "msg")

        assert len(requests) == 4
        assert no_sleep.await_count == 3

    async def test_rate_limit_reports_retry_after(self):
        provider = _real_openai_provider()
        _with_transport(provider, _status(429, headers={"retry-after": "20"}))

        with pytest.raises(ProviderTransientError, match="HTTP 429") as exc_info:
            await provider.generate("sys", "msg")
        assert exc_info.value.retry_after == 20.0

    async def test_read_timeout_is_a_timeout_not_a_connection_failure(self):
        provider = _real_openai_provider("http://localhost:11434/v1", "qwen3-coder:30b", 600)
        _with_transport(provider, _raise(httpx.ReadTimeout))

        with pytest.raises(ProviderTimeoutError) as exc_info:
            await provider.generate("sys", "msg")

        message = str(exc_info.value)
        assert "qwen3-coder:30b" in message
        assert "600s" in message
        assert "Cannot connect" not in message
        assert "Is Ollama running" not in message

    async def test_ollama_unreachable_is_retryable(self):
        from delegatron.agents.providers.openai_provider import OllamaConnectionError

        provider = _real_openai_provider("http://localhost:11434/v1", "qwen3-coder:30b")
        _with_transport(provider, _raise(httpx.ConnectError))

        with pytest.raises(OllamaConnectionError, match="ollama serve") as exc_info:
            await provider.generate("sys", "msg")
        assert exc_info.value.retryable is True

    @pytest.mark.parametrize(
        ("status", "body", "match"),
        [
            (500, {"error": "CUDA error: out of memory"}, "ran out of memory"),
            (
                500,
                {"error": "model requires more system memory (21.4 GiB) than is available"},
                "ran out of memory",
            ),
            (500, {"error": "llama runner: CUDA OOM while loading"}, "ran out of memory"),
            (404, {"error": "model not found"}, "ollama pull"),
        ],
        ids=["out_of_memory", "requires_more_memory", "oom_word", "missing_model"],
    )
    async def test_ollama_oom_and_missing_model_fail_after_one_request(
        self, status, body, match, no_sleep
    ):
        from delegatron.agents.providers.openai_provider import OllamaConnectionError

        provider = _real_openai_provider("http://localhost:11434/v1", "qwen3-coder:30b")
        requests = _with_transport(provider, _status(status, body))

        with pytest.raises(OllamaConnectionError, match=match) as exc_info:
            await retry_with_backoff(provider.generate, "sys", "msg")

        assert exc_info.value.retryable is False
        assert len(requests) == 1

    @pytest.mark.parametrize(
        "error",
        [
            "llama runner process has terminated",
            # "oom" inside a word (a "bloom" model, a "room" message) is no out-of-memory.
            "llama runner for bloom:7b exited: no room left in the batch",
        ],
    )
    async def test_ollama_server_error_is_retryable(self, error):
        from delegatron.agents.providers.openai_provider import OllamaConnectionError

        provider = _real_openai_provider("http://localhost:11434/v1", "qwen3-coder:30b")
        _with_transport(provider, _status(500, {"error": error}))

        with pytest.raises(OllamaConnectionError, match="ollama rm") as exc_info:
            await provider.generate("sys", "msg")
        assert exc_info.value.retryable is True

    @pytest.mark.parametrize("status", [400, 401, 404])
    async def test_agent_invoke_makes_one_call_on_bad_request(self, no_sleep, status):
        # A bad request, a wrong key or an unknown model never gets better on retry.
        from delegatron.agents.implementer import ImplementerAgent

        provider = _real_openai_provider()
        requests = _with_transport(provider, _status(status))

        with pytest.raises(ProviderRequestError):
            await ImplementerAgent(provider=provider, system_prompt="s").invoke({"request": "x"})

        assert len(requests) == 1
        no_sleep.assert_not_awaited()


class TestOpenAIFinishReason:
    async def test_generate_reports_length(self):
        provider = _real_openai_provider()
        _with_transport(provider, lambda request: _openai_completion('{"files": {', "length"))

        response = await provider.generate("sys", "msg")
        assert response.finish_reason == FINISH_REASON_LENGTH

    async def test_generate_reports_stop(self):
        provider = _real_openai_provider()
        _with_transport(provider, lambda request: _openai_completion("{}", "stop"))

        assert (await provider.generate("sys", "msg")).finish_reason == "stop"

    async def test_stream_reports_last_finish_reason(self):
        provider = _real_openai_provider()
        events = [
            _openai_chunk("Hel"),
            _openai_chunk("lo"),
            _openai_chunk(finish_reason="length"),
            "data: [DONE]\n\n",
        ]
        _with_transport(provider, lambda request: _sse_response(events))

        items = [item async for item in provider.stream_generate("sys", "msg")]

        assert items[:-1] == ["Hel", "lo"]
        assert items[-1].content == "Hello"
        assert items[-1].finish_reason == FINISH_REASON_LENGTH


# Ollama and most compatible servers send no prompt_tokens_details (or null).
_WITHOUT_CACHE_DETAILS = (
    _OPENAI_USAGE,
    {**_OPENAI_USAGE, "prompt_tokens_details": None},
    {**_OPENAI_USAGE, "prompt_tokens_details": {"audio_tokens": 0}},
)
_WITHOUT_CACHE_DETAILS_IDS = ("absent", "null", "no_cached_tokens")


class TestOpenAICachedTokens:
    """``prompt_tokens_details.cached_tokens`` becomes ``cache_read_tokens`` (#127)."""

    async def test_generate_reads_cached_tokens(self):
        provider = _real_openai_provider()
        _with_transport(
            provider, lambda request: _openai_completion("{}", "stop", _OPENAI_CACHED_USAGE)
        )

        response = await provider.generate("sys", "msg")
        assert response.cache_read_tokens == 768
        assert response.input_tokens == 2006  # prompt_tokens already counts the cached part
        assert (response.output_tokens, response.cache_write_tokens) == (300, 0)

    @pytest.mark.parametrize("usage", _WITHOUT_CACHE_DETAILS, ids=_WITHOUT_CACHE_DETAILS_IDS)
    async def test_generate_without_cache_details_counts_zero(self, usage):
        provider = _real_openai_provider("http://localhost:11434/v1", "qwen3-coder:30b")
        _with_transport(provider, lambda request: _openai_completion("{}", "stop", usage))

        response = await provider.generate("sys", "msg")
        assert (response.input_tokens, response.cache_read_tokens) == (5, 0)

    async def test_stream_reads_cached_tokens(self):
        provider = _real_openai_provider()
        events = [
            _openai_chunk("{}"),
            _openai_chunk(finish_reason="stop"),
            _openai_usage_chunk(_OPENAI_CACHED_USAGE),
            "data: [DONE]\n\n",
        ]
        _with_transport(provider, lambda request: _sse_response(events))

        final = [item async for item in provider.stream_generate("sys", "msg")][-1]
        assert (final.input_tokens, final.output_tokens, final.cache_read_tokens) == (
            2006,
            300,
            768,
        )

    @pytest.mark.parametrize(
        "usage",
        [*_WITHOUT_CACHE_DETAILS, None],
        ids=[*_WITHOUT_CACHE_DETAILS_IDS, "no_usage_chunk"],
    )
    async def test_stream_without_cache_details_counts_zero(self, usage):
        provider = _real_openai_provider("http://localhost:11434/v1", "qwen3-coder:30b")
        events = [_openai_chunk("{}"), _openai_chunk(finish_reason="stop")]
        if usage is not None:
            events.append(_openai_usage_chunk(usage))
        _with_transport(provider, lambda request: _sse_response([*events, "data: [DONE]\n\n"]))

        final = [item async for item in provider.stream_generate("sys", "msg")][-1]
        assert final.cache_read_tokens == 0
        assert final.input_tokens == (5 if usage else 0)


class TestOpenAIMidStreamFailures:
    async def _consume(self, provider: Any) -> tuple[list[str], Exception]:
        tokens: list[str] = []
        with pytest.raises(ProviderError) as exc_info:
            async for item in provider.stream_generate("sys", "msg"):
                tokens.append(item)
        return tokens, exc_info.value

    async def test_disconnect_after_two_chunks_is_transient(self):
        provider = _real_openai_provider()
        events = [_openai_chunk("Hel"), _openai_chunk("lo")]
        error = httpx.RemoteProtocolError("peer closed connection without sending complete body")
        _with_transport(provider, lambda request: _sse_response(events, error))

        tokens, err = await self._consume(provider)

        assert tokens == ["Hel", "lo"]
        assert type(err) is ProviderTransientError
        assert err.retryable is True

    async def test_in_band_error_payload_is_transient(self):
        provider = _real_openai_provider()
        events = [_openai_chunk("Hel"), 'data: {"error": {"message": "model crashed"}}\n\n']
        _with_transport(provider, lambda request: _sse_response(events))

        tokens, err = await self._consume(provider)

        assert tokens == ["Hel"]
        assert type(err) is ProviderTransientError
        assert "model crashed" in str(err)

    async def test_read_timeout_mid_stream_is_a_timeout(self):
        provider = _real_openai_provider(timeout=45)
        error = httpx.ReadTimeout("timed out")
        _with_transport(provider, lambda request: _sse_response([_openai_chunk("Hel")], error))

        _, err = await self._consume(provider)
        assert type(err) is ProviderTimeoutError
        assert "45s" in str(err)

    async def test_error_before_streaming_is_classified_by_status(self):
        provider = _real_openai_provider()
        _with_transport(provider, _status(401))

        _, err = await self._consume(provider)
        assert type(err) is ProviderRequestError
        assert "HTTP 401" in str(err)


# --- Anthropic adapter (real SDK, mock transport) ---


def _anthropic_provider(timeout: float = 30, model: str = "claude-sonnet-4-6") -> Any:
    pytest.importorskip("anthropic")
    from delegatron.agents.providers.anthropic import AnthropicProvider

    return AnthropicProvider(api_key="k", model=model, timeout_seconds=timeout)


def _anthropic_message(stop_reason: str, usage: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-x",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "patchset", "input": {}}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage or {"input_tokens": 10, "output_tokens": 20},
        },
    )


_ANTHROPIC_CACHED_USAGE = {
    "input_tokens": 100,
    "cache_read_input_tokens": 900,
    "cache_creation_input_tokens": 0,
    "output_tokens": 50,
}


def _anthropic_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps({'type': event_type, **data})}\n\n"


_ANTHROPIC_STREAM_START = [
    _anthropic_event(
        "message_start",
        {
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-x",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 1},
            }
        },
    ),
    _anthropic_event(
        "content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}
    ),
    _anthropic_event(
        "content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Hel"}}
    ),
    _anthropic_event(
        "content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "lo"}}
    ),
]


class TestAnthropicAdapter:
    @pytest.mark.parametrize(
        ("stop_reason", "expected"),
        [("max_tokens", FINISH_REASON_LENGTH), ("tool_use", "tool_use")],
    )
    async def test_generate_normalizes_stop_reason(self, stop_reason, expected):
        provider = _anthropic_provider()
        _with_transport(provider, lambda request: _anthropic_message(stop_reason))

        response = await provider.generate("sys", "msg", json_schema={"title": "PatchSet"})
        assert response.finish_reason == expected
        assert response.content == "{}"

    async def test_overloaded_is_transient_with_retry_after(self):
        provider = _anthropic_provider()
        body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        _with_transport(provider, _status(529, body, {"retry-after": "3"}))

        with pytest.raises(ProviderTransientError, match="HTTP 529") as exc_info:
            await provider.generate("sys", "msg")
        assert exc_info.value.retry_after == 3.0

    async def test_bad_request_fails_after_one_request(self, no_sleep):
        provider = _anthropic_provider()
        requests = _with_transport(provider, _status(400))

        with pytest.raises(ProviderRequestError):
            await retry_with_backoff(provider.generate, "sys", "msg")
        assert len(requests) == 1

    async def test_timeout_and_connection_errors(self):
        provider = _anthropic_provider(timeout=50)
        _with_transport(provider, _raise(httpx.ReadTimeout))
        with pytest.raises(ProviderTimeoutError, match="50s"):
            await provider.generate("sys", "msg")

        _with_transport(provider, _raise(httpx.ConnectError))
        with pytest.raises(ProviderTransientError, match=r"api\.anthropic\.com"):
            await provider.generate("sys", "msg")

    async def test_stream_reports_stop_reason_from_message_delta(self):
        provider = _anthropic_provider()
        events = [
            *_ANTHROPIC_STREAM_START,
            _anthropic_event("content_block_stop", {"index": 0}),
            _anthropic_event(
                "message_delta",
                {
                    "delta": {"stop_reason": "max_tokens", "stop_sequence": None},
                    "usage": {"output_tokens": 9},
                },
            ),
            _anthropic_event("message_stop", {}),
        ]
        _with_transport(provider, lambda request: _sse_response(events))

        items = [item async for item in provider.stream_generate("sys", "msg")]

        assert items[:-1] == ["Hel", "lo"]
        final = items[-1]
        assert final.content == "Hello"
        assert final.finish_reason == FINISH_REASON_LENGTH
        assert (final.input_tokens, final.output_tokens) == (11, 9)

    @pytest.mark.parametrize(
        "tail",
        [
            {"error": httpx.RemoteProtocolError("peer closed connection")},
            {
                "events": [
                    _anthropic_event(
                        "error", {"error": {"type": "overloaded_error", "message": "Overloaded"}}
                    )
                ]
            },
        ],
        ids=["disconnect", "error_event"],
    )
    async def test_failure_after_two_chunks_is_transient(self, tail):
        provider = _anthropic_provider()
        events = [*_ANTHROPIC_STREAM_START, *tail.get("events", [])]
        _with_transport(provider, lambda request: _sse_response(events, tail.get("error")))

        tokens: list[str] = []
        with pytest.raises(ProviderTransientError):
            async for item in provider.stream_generate("sys", "msg"):
                tokens.append(item)
        assert tokens == ["Hel", "lo"]

    async def test_stream_rejected_before_start_is_classified_by_status(self):
        provider = _anthropic_provider()
        _with_transport(provider, _status(400))

        with pytest.raises(ProviderRequestError):
            async for _ in provider.stream_generate("sys", "msg"):
                pass

    @pytest.mark.parametrize(
        ("temperature", "sent"), [(1.5, 1.0), (2.0, 1.0), (0.7, 0.7), (0.0, 0.0), (-0.5, 0.0)]
    )
    def test_temperature_is_clamped_to_anthropic_range(self, temperature, sent):
        kwargs = _anthropic_provider()._build_request_kwargs("sys", "msg", None, 100, temperature)
        assert kwargs["temperature"] == sent

    async def test_generate_sends_the_clamped_temperature(self):
        """A role temperature of 1.5 (valid for OpenAI/Gemini) no longer gets an HTTP 400."""
        provider = _anthropic_provider()
        requests = _with_transport(provider, lambda request: _anthropic_message("end_turn"))

        await provider.generate("sys", "msg", temperature=1.5)
        assert json.loads(requests[0].content)["temperature"] == 1.0

    @pytest.mark.parametrize("model", ["claude-sonnet-5-5", "anthropic.claude-sonnet-5-5"])
    def test_no_temperature_or_forced_tool_for_models_that_reject_them(self, model):
        """Sonnet 5.5 returns HTTP 400 for a non-default temperature and a forced tool_choice."""
        schema = {"title": "Work Plan", "type": "object", "properties": {}}
        kwargs = _anthropic_provider(model=model)._build_request_kwargs(
            "sys", "msg", schema, 100, 0.0
        )
        assert "temperature" not in kwargs
        assert kwargs["tool_choice"] == {"type": "auto"}
        assert kwargs["tools"][0]["name"] == "work_plan"

    @pytest.mark.parametrize(
        ("model", "temperature", "forced"),
        [
            ("claude-sonnet-4-6", True, True),
            ("claude-3-5-sonnet-latest", True, True),
            ("claude-opus-4-8", False, True),
            ("claude-sonnet-5", False, True),
            ("claude-opus-5-5", False, False),
            ("claude-fable-5-1", False, False),
            ("claude-next-model", False, False),
        ],
    )
    def test_request_shape_follows_the_model(self, model, temperature, forced):
        schema = {"title": "plan", "type": "object", "properties": {}}
        kwargs = _anthropic_provider(model=model)._build_request_kwargs(
            "sys", "msg", schema, 100, 0.0
        )
        assert ("temperature" in kwargs) is temperature
        assert (kwargs["tool_choice"]["type"] == "tool") is forced

    @pytest.mark.parametrize(
        ("usage", "expected"),
        [
            (_ANTHROPIC_CACHED_USAGE, (1000, 900, 0)),
            (
                {
                    **_ANTHROPIC_CACHED_USAGE,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 700,
                },
                (800, 0, 700),
            ),
            ({"input_tokens": 10, "output_tokens": 20}, (10, 0, 0)),
            (
                {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": None},
                (10, 0, 0),
            ),
        ],
        ids=["cache_read", "cache_write", "no_cache_fields", "null_cache_fields"],
    )
    async def test_generate_counts_cached_prompt_tokens(self, usage, expected):
        provider = _anthropic_provider()
        _with_transport(provider, lambda request: _anthropic_message("end_turn", usage))

        response = await provider.generate("sys", "msg")
        assert (
            response.input_tokens,
            response.cache_read_tokens,
            response.cache_write_tokens,
        ) == expected

    async def test_stream_counts_cached_prompt_tokens(self):
        provider = _anthropic_provider()
        start = _anthropic_event(
            "message_start",
            {
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {**_ANTHROPIC_CACHED_USAGE, "output_tokens": 1},
                }
            },
        )
        events = [
            start,
            *_ANTHROPIC_STREAM_START[1:],
            _anthropic_event("content_block_stop", {"index": 0}),
            _anthropic_event(
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 50},
                },
            ),
            _anthropic_event("message_stop", {}),
        ]
        _with_transport(provider, lambda request: _sse_response(events))

        final = [item async for item in provider.stream_generate("sys", "msg")][-1]
        assert (final.input_tokens, final.cache_read_tokens, final.output_tokens) == (1000, 900, 50)


# --- Google adapter ---


def _google_provider(timeout: float = 30) -> Any:
    pytest.importorskip("google.genai")
    from delegatron.agents.providers.google import GoogleProvider

    return GoogleProvider(api_key="k", model="gemini-2.5-flash", timeout_seconds=timeout)


def _google_body(finish_reason: str | None = "STOP", text: str = "{}") -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": [{"text": text}]}}
    if finish_reason:
        candidate["finishReason"] = finish_reason
    return {
        "candidates": [candidate],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5},
    }


def _stub_google_http(body: dict[str, Any]):
    """Replace the SDK's HTTP layer; returns (patcher, captured request dicts)."""
    from types import SimpleNamespace

    captured: list[dict[str, Any]] = []

    async def async_request(self, http_method, path, request_dict, http_options=None):
        captured.append(request_dict)
        return SimpleNamespace(headers={}, body=json.dumps(body))

    target = "google.genai._api_client.BaseApiClient.async_request"
    return patch(target, async_request), captured


class TestGoogleAdapter:
    @SCHEMAS
    def test_build_config_sends_json_schema(self, model_cls):
        schema = model_cls.model_json_schema()
        config = _google_provider()._build_config("sys", schema, 4096, 0.0)
        assert config.response_json_schema == schema
        assert config.response_schema is None
        assert config.response_mime_type == "application/json"

    def test_build_config_without_schema_is_plain_text(self):
        config = _google_provider()._build_config("sys", None, 4096, 0.0)
        assert config.response_json_schema is None
        assert config.response_mime_type is None

    @SCHEMAS
    async def test_generate_builds_a_valid_request(self, model_cls):
        provider = _google_provider()
        patcher, captured = _stub_google_http(_google_body())

        with patcher:
            response = await provider.generate(
                "sys", "msg", json_schema=model_cls.model_json_schema()
            )

        generation_config = captured[0]["generationConfig"]
        assert "responseJsonSchema" in generation_config
        assert "responseSchema" not in generation_config
        assert response.content == "{}"
        assert (response.input_tokens, response.output_tokens) == (3, 5)

    @pytest.mark.parametrize(
        ("usage", "expected"),
        [
            (
                {
                    "promptTokenCount": 2006,
                    "cachedContentTokenCount": 1024,
                    "candidatesTokenCount": 300,
                    "thoughtsTokenCount": 120,
                },
                (2006, 420, 1024),
            ),
            ({"promptTokenCount": 3, "candidatesTokenCount": 5}, (3, 5, 0)),
            ({}, (0, 0, 0)),
        ],
        ids=["implicit_cache_hit", "no_cache", "empty_usage"],
    )
    async def test_generate_counts_cached_and_thinking_tokens(self, usage, expected):
        """``cached_content_token_count`` is the cached part of the prompt (#127)."""
        provider = _google_provider()
        patcher, _ = _stub_google_http({**_google_body(), "usageMetadata": usage})

        with patcher:
            response = await provider.generate("sys", "msg")
        assert (
            response.input_tokens,
            response.output_tokens,
            response.cache_read_tokens,
        ) == expected
        assert response.cache_write_tokens == 0

    async def test_generate_without_usage_metadata(self):
        provider = _google_provider()
        body = _google_body()
        del body["usageMetadata"]
        patcher, _ = _stub_google_http(body)

        with patcher:
            response = await provider.generate("sys", "msg")
        assert (response.input_tokens, response.output_tokens, response.cache_read_tokens) == (
            0,
            0,
            0,
        )

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (_google_body("MAX_TOKENS", text=""), FINISH_REASON_LENGTH),
            (_google_body("STOP"), "STOP"),
            (_google_body(None), ""),
            ({"candidates": []}, ""),
            ({}, ""),
        ],
        ids=["max_tokens", "stop", "no_reason", "empty_candidates", "no_candidates"],
    )
    async def test_finish_reason(self, body, expected):
        provider = _google_provider()
        patcher, _ = _stub_google_http(body)

        with patcher:
            response = await provider.generate("sys", "msg")
        assert response.finish_reason == expected

    def _fail_with(self, provider: Any, error: Exception) -> AsyncMock:
        generate_content = AsyncMock(side_effect=error)
        provider._client = MagicMock()
        provider._client.aio.models.generate_content = generate_content
        return generate_content

    async def test_client_side_value_error_is_not_retried(self, no_sleep):
        provider = _google_provider()
        error = ValueError("additionalProperties is not supported in the Gemini API.")
        generate_content = self._fail_with(provider, error)

        with pytest.raises(ValueError, match="additionalProperties"):
            await retry_with_backoff(provider.generate, "sys", "msg")
        assert generate_content.await_count == 1
        no_sleep.assert_not_awaited()

    async def test_agent_invoke_raises_value_error_after_one_call(self, no_sleep):
        from delegatron.agents.implementer import ImplementerAgent

        provider = _google_provider()
        generate_content = self._fail_with(provider, ValueError("client-side schema error"))

        with pytest.raises(ValueError, match="client-side schema error"):
            await ImplementerAgent(provider=provider, system_prompt="s").invoke({"request": "x"})
        assert generate_content.await_count == 1

    @pytest.mark.parametrize(
        ("code", "expected"), [(400, ProviderRequestError), (503, ProviderTransientError)]
    )
    async def test_api_errors_become_provider_errors(self, code, expected):
        provider = _google_provider()
        from google.genai import errors

        body = {"error": {"code": code, "message": "nope", "status": "X"}}
        error_cls = errors.ClientError if code < 500 else errors.ServerError
        self._fail_with(provider, error_cls(code, body))

        with pytest.raises(expected) as exc_info:
            await provider.generate("sys", "msg")
        assert isinstance(exc_info.value, RuntimeError)
        assert "gemini-2.5-flash" in str(exc_info.value)

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (TimeoutError(), ProviderTimeoutError),
            (httpx.ReadTimeout("slow"), ProviderTimeoutError),
            (httpx.ConnectError("refused"), ProviderTransientError),
            (ConnectionResetError("reset"), ProviderTransientError),
        ],
    )
    async def test_network_errors(self, error, expected):
        provider = _google_provider(timeout=60)
        self._fail_with(provider, error)

        with pytest.raises(expected):
            await provider.generate("sys", "msg")
