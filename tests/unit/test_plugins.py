"""Tests for the plugin system."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from delegatron.plugins import (
    HOOK_EVENTS,
    HookManager,
    PluginError,
    PluginInfo,
    PluginRegistry,
    PluginType,
    _import_entry_point,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> PluginRegistry:
    return PluginRegistry()


@pytest.fixture()
def hook_manager() -> HookManager:
    return HookManager()


def _make_info(
    name: str = "test-plugin",
    version: str = "1.0.0",
    plugin_type: PluginType = PluginType.AGENT,
    entry_point: str = "some.module.SomeClass",
    description: str = "A test plugin",
    author: str = "tester",
) -> PluginInfo:
    return PluginInfo(
        name=name,
        version=version,
        plugin_type=plugin_type,
        entry_point=entry_point,
        description=description,
        author=author,
    )


# ---------------------------------------------------------------------------
# PluginType enum
# ---------------------------------------------------------------------------


class TestPluginType:
    def test_values(self) -> None:
        assert PluginType.AGENT.value == "agent"
        assert PluginType.PROVIDER.value == "provider"
        assert PluginType.SANDBOX.value == "sandbox"
        assert PluginType.HOOK.value == "hook"

    def test_members(self) -> None:
        assert set(PluginType) == {
            PluginType.AGENT,
            PluginType.PROVIDER,
            PluginType.SANDBOX,
            PluginType.HOOK,
        }


# ---------------------------------------------------------------------------
# PluginInfo dataclass
# ---------------------------------------------------------------------------


class TestPluginInfo:
    def test_creation(self) -> None:
        info = _make_info()
        assert info.name == "test-plugin"
        assert info.version == "1.0.0"
        assert info.plugin_type == PluginType.AGENT
        assert info.entry_point == "some.module.SomeClass"

    def test_defaults(self) -> None:
        info = PluginInfo(
            name="minimal",
            version="0.1.0",
            plugin_type=PluginType.HOOK,
            entry_point="a.b.C",
        )
        assert info.description == ""
        assert info.author == ""


# ---------------------------------------------------------------------------
# PluginRegistry
# ---------------------------------------------------------------------------


class TestPluginRegistry:
    def test_register_and_get(self, registry: PluginRegistry) -> None:
        info = _make_info()
        registry.register(info)
        assert registry.get("test-plugin") is info

    def test_register_duplicate_raises(self, registry: PluginRegistry) -> None:
        registry.register(_make_info())
        with pytest.raises(PluginError, match="already registered"):
            registry.register(_make_info())

    def test_get_missing_raises(self, registry: PluginRegistry) -> None:
        with pytest.raises(KeyError, match="not found"):
            registry.get("nope")

    def test_unregister(self, registry: PluginRegistry) -> None:
        registry.register(_make_info())
        registry.unregister("test-plugin")
        with pytest.raises(KeyError):
            registry.get("test-plugin")

    def test_unregister_missing_raises(self, registry: PluginRegistry) -> None:
        with pytest.raises(KeyError, match="not found"):
            registry.unregister("nope")

    def test_list_all(self, registry: PluginRegistry) -> None:
        registry.register(_make_info(name="a", plugin_type=PluginType.AGENT))
        registry.register(_make_info(name="b", plugin_type=PluginType.PROVIDER))
        assert len(registry.list()) == 2

    def test_list_filtered(self, registry: PluginRegistry) -> None:
        registry.register(_make_info(name="a", plugin_type=PluginType.AGENT))
        registry.register(_make_info(name="b", plugin_type=PluginType.PROVIDER))
        registry.register(_make_info(name="c", plugin_type=PluginType.AGENT))
        agents = registry.list(PluginType.AGENT)
        assert len(agents) == 2
        assert all(p.plugin_type == PluginType.AGENT for p in agents)

    def test_list_empty_type(self, registry: PluginRegistry) -> None:
        registry.register(_make_info(name="a", plugin_type=PluginType.AGENT))
        assert registry.list(PluginType.SANDBOX) == []

    def test_load_success(self, registry: PluginRegistry) -> None:
        info = _make_info(entry_point="json.JSONEncoder")
        registry.register(info)
        cls = registry.load("test-plugin")
        import json

        assert cls is json.JSONEncoder

    def test_load_missing_raises(self, registry: PluginRegistry) -> None:
        with pytest.raises(KeyError):
            registry.load("nonexistent")

    def test_load_bad_module_raises(self, registry: PluginRegistry) -> None:
        info = _make_info(entry_point="no_such_module_xyz.Foo")
        registry.register(info)
        with pytest.raises(PluginError, match="Could not import"):
            registry.load("test-plugin")

    def test_load_bad_attr_raises(self, registry: PluginRegistry) -> None:
        info = _make_info(entry_point="json.NoSuchThing")
        registry.register(info)
        with pytest.raises(PluginError, match="has no attribute"):
            registry.load("test-plugin")


# ---------------------------------------------------------------------------
# PluginRegistry.discover — entry points
# ---------------------------------------------------------------------------


class TestPluginDiscoverEntryPoints:
    def test_discover_entry_points(self, registry: PluginRegistry) -> None:
        mock_info = _make_info(name="ep-plugin")
        mock_ep = MagicMock()
        mock_ep.name = "ep-plugin"
        mock_ep.load.return_value = mock_info

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            found = registry.discover()

        assert any(p.name == "ep-plugin" for p in found)
        assert registry.get("ep-plugin") is mock_info

    def test_discover_entry_point_callable(self, registry: PluginRegistry) -> None:
        mock_info = _make_info(name="callable-ep")

        def factory() -> PluginInfo:
            return mock_info

        mock_ep = MagicMock()
        mock_ep.name = "callable-ep"
        mock_ep.load.return_value = factory

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            found = registry.discover()

        assert any(p.name == "callable-ep" for p in found)

    def test_discover_skips_bad_entry_point(self, registry: PluginRegistry) -> None:
        mock_ep = MagicMock()
        mock_ep.name = "bad-ep"
        mock_ep.load.side_effect = ImportError("boom")

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            found = registry.discover()

        assert found == []


# ---------------------------------------------------------------------------
# PluginRegistry.discover — local directories
# ---------------------------------------------------------------------------


class TestPluginDiscoverLocal:
    @pytest.fixture(autouse=True)
    def _no_installed_plugins(self) -> Iterator[None]:
        """Plugins of installed packages (entry points) must not leak into these tests."""
        with patch("importlib.metadata.entry_points", return_value=[]):
            yield

    def test_discover_local_py_module(self, registry: PluginRegistry, tmp_path: Path) -> None:
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text(
            "from delegatron.plugins import PluginInfo, PluginType\n"
            "plugin_info = PluginInfo(\n"
            '    name="local-plug",\n'
            '    version="0.1.0",\n'
            "    plugin_type=PluginType.SANDBOX,\n"
            '    entry_point="json.JSONEncoder",\n'
            ")\n"
        )
        found = registry.discover(search_paths=[tmp_path])
        assert any(p.name == "local-plug" for p in found)

    def test_discover_local_skips_underscored(
        self, registry: PluginRegistry, tmp_path: Path
    ) -> None:
        (tmp_path / "_private.py").write_text("plugin_info = 'not a real one'")
        found = registry.discover(search_paths=[tmp_path])
        assert found == []

    def test_discover_local_skips_nondir(self, registry: PluginRegistry, tmp_path: Path) -> None:
        fake_path = tmp_path / "does_not_exist"
        found = registry.discover(search_paths=[fake_path])
        assert found == []


# ---------------------------------------------------------------------------
# HookManager
# ---------------------------------------------------------------------------


class TestHookManager:
    def test_register_and_emit(self, hook_manager: HookManager) -> None:
        called_with: dict[str, object] = {}

        def on_plan(**kwargs: object) -> None:
            called_with.update(kwargs)

        hook_manager.register_hook("before_plan", on_plan)
        hook_manager.emit("before_plan", task="build feature")
        assert called_with == {"task": "build feature"}

    def test_multiple_callbacks(self, hook_manager: HookManager) -> None:
        calls: list[str] = []
        hook_manager.register_hook("after_plan", lambda **kw: calls.append("a"))
        hook_manager.register_hook("after_plan", lambda **kw: calls.append("b"))
        hook_manager.emit("after_plan")
        assert calls == ["a", "b"]

    def test_emit_no_callbacks(self, hook_manager: HookManager) -> None:
        # Should not raise
        hook_manager.emit("on_approve")

    def test_register_invalid_event(self, hook_manager: HookManager) -> None:
        with pytest.raises(ValueError, match="Unknown hook event"):
            hook_manager.register_hook("bad_event", lambda **kw: None)

    def test_emit_invalid_event(self, hook_manager: HookManager) -> None:
        with pytest.raises(ValueError, match="Unknown hook event"):
            hook_manager.emit("bad_event")

    def test_callback_error_does_not_stop_others(self, hook_manager: HookManager) -> None:
        calls: list[str] = []

        def bad(**kwargs: object) -> None:
            raise RuntimeError("fail")

        def good(**kwargs: object) -> None:
            calls.append("ok")

        hook_manager.register_hook("on_error", bad)
        hook_manager.register_hook("on_error", good)
        hook_manager.emit("on_error")
        assert calls == ["ok"]

    def test_all_events_supported(self) -> None:
        expected = {
            "before_plan",
            "after_plan",
            "before_implement",
            "after_implement",
            "before_review",
            "after_review",
            "on_approve",
            "on_reject",
            "on_error",
        }
        assert expected == HOOK_EVENTS


# ---------------------------------------------------------------------------
# _import_entry_point helper
# ---------------------------------------------------------------------------


class TestImportEntryPoint:
    def test_valid_path(self) -> None:
        import json

        assert _import_entry_point("json.JSONEncoder") is json.JSONEncoder

    def test_no_dot_raises(self) -> None:
        with pytest.raises(PluginError, match="Invalid entry point"):
            _import_entry_point("nodot")

    def test_bad_module_raises(self) -> None:
        with pytest.raises(PluginError, match="Could not import"):
            _import_entry_point("nonexistent_mod_xyz.Cls")

    def test_bad_attr_raises(self) -> None:
        with pytest.raises(PluginError, match="has no attribute"):
            _import_entry_point("json.NotReal")
