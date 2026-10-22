"""Tests for vendor-neutral API normalization utilities."""

import pytest

from delegatron.utils.provider import is_local_base_url, is_local_host, normalize_messages


class TestNormalizeMessages:
    """Tests for the normalize_messages function."""

    def test_basic_structure(self):
        result = normalize_messages("You are a helpful assistant.", "Hello!")
        assert "system" in result
        assert "messages" in result

    def test_system_prompt_stored(self):
        result = normalize_messages("System prompt here", "User message")
        assert result["system"] == "System prompt here"

    def test_user_message_in_messages_list(self):
        result = normalize_messages("sys", "What is 2+2?")
        messages = result["messages"]
        assert isinstance(messages, list)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "What is 2+2?"

    def test_empty_strings(self):
        result = normalize_messages("", "")
        assert result["system"] == ""
        assert result["messages"][0]["content"] == ""

    def test_multiline_system_prompt(self):
        prompt = "Line 1\nLine 2\nLine 3"
        result = normalize_messages(prompt, "hi")
        assert result["system"] == prompt

    def test_multiline_user_message(self):
        msg = "Please do:\n1. First\n2. Second"
        result = normalize_messages("sys", msg)
        assert result["messages"][0]["content"] == msg

    def test_special_characters(self):
        result = normalize_messages("sys <b>bold</b>", "user {json: true}")
        assert result["system"] == "sys <b>bold</b>"
        assert result["messages"][0]["content"] == "user {json: true}"

    def test_return_type_is_dict(self):
        result = normalize_messages("a", "b")
        assert isinstance(result, dict)

    def test_unicode_content(self):
        result = normalize_messages("Rolle: Helfer", "Was ist das? \u00fc\u00f6\u00e4")
        assert result["system"] == "Rolle: Helfer"
        assert "\u00fc\u00f6\u00e4" in result["messages"][0]["content"]


class TestIsLocalBaseUrl:
    """Tests for the is_local_base_url host check."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:11434/v1",
            "http://127.0.0.1:1234/v1",
            "http://[::1]:11434/v1",
            "http://host.docker.internal:11434/v1",
            "HTTP://LOCALHOST:11434",
            "localhost:11434/v1",
            # The local network (D1): RFC 1918, link-local, CGNAT, IPv6 ULA ...
            "http://192.168.1.20:11434/v1",
            "http://10.0.0.5:11434/v1",
            "http://172.16.4.2:8000/v1",
            "http://169.254.10.1:11434",
            "http://100.101.102.103:11434/v1",
            "http://[fd12:3456:789a::1]:11434/v1",
            "http://[fe80::1]:11434/v1",
            "http://[::ffff:192.168.1.5]:11434/v1",
            # ... and LAN-only names
            "http://gpu-box.local:11434/v1",
            "http://ollama.lan:11434/v1",
            "http://ollama.internal:11434/v1",
            "http://llm.home.arpa:11434/v1",
            "http://ollama.localhost:11434/v1",
            "http://GPU-BOX.LOCAL.:11434/v1",
            # single-label names: compose/k8s services and LAN machines
            "http://ollama:11434/v1",
            "http://gpu-box:11434/v1",
        ],
    )
    def test_local_hosts(self, url: str):
        assert is_local_base_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "https://api.openai.com/v1",
            "http://localhost.example.com/v1",
            "https://example.com/localhost/v1",
            "http://8.8.8.8:11434/v1",
            "http://100.128.0.1:11434/v1",  # just outside 100.64.0.0/10
            "http://[2001:4860:4860::8888]/v1",
            "https://ollama.example.lan.com/v1",
            "https://internal.example.com/v1",
            "http://[::1/v1",  # unparsable
        ],
    )
    def test_remote_hosts(self, url: str):
        assert is_local_base_url(url) is False

    @pytest.mark.parametrize(
        ("host", "local"),
        [("192.168.0.1", True), ("[::1]", True), ("", False), ("example.com", False)],
    )
    def test_is_local_host(self, host: str, local: bool):
        assert is_local_host(host) is local
