"""Plugin system for custom agents, providers, sandboxes, and lifecycle hooks."""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PluginType(Enum):
    """Supported plugin categories."""

    AGENT = "agent"
    PROVIDER = "provider"
    SANDBOX = "sandbox"
    HOOK = "hook"


# Valid hook event names
HOOK_EVENTS = frozenset(
    {
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
)


@dataclass
class PluginInfo:
    """Metadata describing a registered plugin."""

    name: str
    version: str
    plugin_type: PluginType
    entry_point: str
    description: str = ""
    author: str = ""


class PluginError(Exception):
    """Raised for plugin registration and loading errors."""


class PluginRegistry:
    """Central registry for discovering, registering, and loading plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginInfo] = {}

    def register(self, info: PluginInfo) -> None:
        """Register a plugin.

        Raises:
            PluginError: If a plugin with the same name is already registered.
        """
        if info.name in self._plugins:
            raise PluginError(f"Plugin already registered: {info.name!r}")
        self._plugins[info.name] = info
        logger.info("Registered plugin %r (%s)", info.name, info.plugin_type.value)

    def unregister(self, name: str) -> None:
        """Remove a plugin from the registry.

        Raises:
            KeyError: If the plugin is not registered.
        """
        if name not in self._plugins:
            raise KeyError(f"Plugin not found: {name!r}")
        del self._plugins[name]
        logger.info("Unregistered plugin %r", name)

    def get(self, name: str) -> PluginInfo:
        """Retrieve plugin info by name.

        Raises:
            KeyError: If the plugin is not registered.
        """
        if name not in self._plugins:
            raise KeyError(f"Plugin not found: {name!r}")
        return self._plugins[name]

    def list(self, plugin_type: PluginType | None = None) -> builtins.list[PluginInfo]:
        """List registered plugins, optionally filtered by type."""
        if plugin_type is None:
            return builtins.list(self._plugins.values())
        return [p for p in self._plugins.values() if p.plugin_type == plugin_type]

    def load(self, name: str) -> Any:
        """Dynamically import and return the plugin class/function.

        The ``entry_point`` field is a dotted Python path where the last
        component is the attribute name (e.g. ``mypackage.module.MyClass``).

        Raises:
            KeyError: If the plugin is not registered.
            PluginError: If the import or attribute lookup fails.
        """
        info = self.get(name)
        return _import_entry_point(info.entry_point)

    def discover(
        self, search_paths: builtins.list[Path] | None = None
    ) -> builtins.list[PluginInfo]:
        """Discover plugins from installed packages and optional local directories.

        Package-level discovery uses ``importlib.metadata.entry_points`` with the
        group ``delegatron.plugins``.  Each entry point value must be a dotted
        path to a ``PluginInfo`` instance or a callable that returns one.

        Local directory discovery scans for ``*.plugin.toml`` files (reserved
        for future use) and Python modules exposing a ``plugin_info`` attribute.

        Returns:
            List of newly discovered ``PluginInfo`` objects (also registered).
        """
        discovered: list[PluginInfo] = []

        # --- Package entry-points ------------------------------------------
        discovered.extend(self._discover_entry_points())

        # --- Local search paths --------------------------------------------
        if search_paths:
            discovered.extend(self._discover_local(search_paths))

        return discovered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _discover_entry_points(self) -> builtins.list[PluginInfo]:
        """Scan installed packages for delegatron.plugins entry points."""
        found: list[PluginInfo] = []
        try:
            from importlib.metadata import entry_points

            eps = entry_points(group="delegatron.plugins")
        except Exception:
            logger.debug("No entry points found for delegatron.plugins")
            return found

        for ep in eps:
            try:
                obj = ep.load()
                info = obj if isinstance(obj, PluginInfo) else obj()
                if not isinstance(info, PluginInfo):
                    logger.warning(
                        "Entry point %r did not resolve to PluginInfo, skipping", ep.name
                    )
                    continue
                if info.name not in self._plugins:
                    self.register(info)
                    found.append(info)
            except Exception:
                logger.warning("Failed to load entry point %r", ep.name, exc_info=True)

        return found

    def _discover_local(self, search_paths: builtins.list[Path]) -> builtins.list[PluginInfo]:
        """Scan local directories for Python modules with a ``plugin_info`` attr."""
        found: list[PluginInfo] = []
        for path in search_paths:
            if not path.is_dir():
                continue
            for py_file in sorted(path.glob("*.py")):
                module_name = py_file.stem
                if module_name.startswith("_"):
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(module_name, py_file)
                    if spec is None or spec.loader is None:
                        continue
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    info = getattr(mod, "plugin_info", None)
                    if isinstance(info, PluginInfo) and info.name not in self._plugins:
                        self.register(info)
                        found.append(info)
                except Exception:
                    logger.warning("Failed to load plugin from %s", py_file, exc_info=True)

        return found


class HookManager:
    """Manages lifecycle hook callbacks."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[Callable[..., Any]]] = {event: [] for event in HOOK_EVENTS}

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        """Register a callback for a lifecycle event.

        Raises:
            ValueError: If the event name is not recognised.
        """
        if event not in HOOK_EVENTS:
            raise ValueError(f"Unknown hook event: {event!r}. Valid events: {sorted(HOOK_EVENTS)}")
        self._hooks[event].append(callback)
        logger.debug("Hook registered for event %r: %r", event, callback)

    def emit(self, event: str, **kwargs: Any) -> None:
        """Invoke all registered callbacks for *event*.

        Callbacks are called in registration order.  If a callback raises an
        exception it is logged and the remaining callbacks still execute.

        Raises:
            ValueError: If the event name is not recognised.
        """
        if event not in HOOK_EVENTS:
            raise ValueError(f"Unknown hook event: {event!r}. Valid events: {sorted(HOOK_EVENTS)}")
        for callback in self._hooks[event]:
            try:
                callback(**kwargs)
            except Exception:
                logger.exception("Hook callback %r failed for event %r", callback, event)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _import_entry_point(dotted_path: str) -> Any:
    """Import a dotted path and return the final attribute.

    Example: ``"delegatron.agents.architect.ArchitectAgent"`` imports
    the ``delegatron.agents.architect`` module and returns the
    ``ArchitectAgent`` attribute.
    """
    parts = dotted_path.rsplit(".", 1)
    if len(parts) != 2:
        raise PluginError(
            f"Invalid entry point {dotted_path!r}: expected 'module.attribute' format"
        )
    module_path, attr_name = parts
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise PluginError(f"Could not import module {module_path!r}: {exc}") from exc
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise PluginError(f"Module {module_path!r} has no attribute {attr_name!r}") from exc
