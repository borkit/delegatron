# Delegatron Plugin System

Extend Delegatron with custom agents, providers, sandboxes, and lifecycle hooks.

> **Status:** `delegatron.plugins` is a library API. The pipeline, CLI and server do not load plugins or emit hook events yet: `PluginRegistry.discover()` and `HookManager.emit()` run only when your own code calls them, and provider/sandbox/agent selection (`agents/providers/registry.py`, `factories/`) does not consult the plugin registry.

## Plugin Types

| Type | Description | Example Use Case |
|------|-------------|-----------------|
| `AGENT` | Custom agent role | Add a security auditor agent |
| `PROVIDER` | Custom LLM provider adapter | Connect to a proprietary model API |
| `SANDBOX` | Custom sandbox backend | Run tests in a Kubernetes pod |
| `HOOK` | Lifecycle callback | Post Slack notifications on completion |

## Creating a Plugin

### 1. Write Your Plugin

Create a Python class or function and expose a module-level `plugin_info` (a `PluginInfo` instance). `entry_point` is a dotted `module.attribute` path; `PluginRegistry.load()` splits it at the last dot, so the `module:attribute` form does not load:

```python
# my_plugin/provider.py
from delegatron.agents.providers.base import BaseProvider, ProviderResponse
from delegatron.plugins import PluginInfo, PluginType

class MyCustomProvider(BaseProvider):
    """Custom provider for a proprietary model API."""

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        # Your implementation here
        ...

# Required: module-level plugin_info for discovery
plugin_info = PluginInfo(
    name="my-custom-provider",
    version="1.0.0",
    plugin_type=PluginType.PROVIDER,
    entry_point="my_plugin.provider.MyCustomProvider",
    description="Custom provider for proprietary model API",
    author="Your Name",
)
```

### 2. Register via Entry Points (Recommended)

Add an entry point in your package's `pyproject.toml`. The entry point must resolve to a `PluginInfo` instance (or a callable that returns one), not to the plugin class:

```toml
[project.entry-points."delegatron.plugins"]
my_custom_provider = "my_plugin.provider:plugin_info"
```

`PluginRegistry.discover()` registers the plugins of installed packages from the `delegatron.plugins` entry-point group; Delegatron does not call it on startup.

### 3. Register via Local Discovery (Alternative)

Place your plugin module in a directory and pass it as a search path:

```python
from pathlib import Path
from delegatron.plugins import PluginRegistry

registry = PluginRegistry()
discovered = registry.discover(search_paths=[Path("./my_plugins")])
```

The discovery imports each `*.py` file in those directories (names starting with `_` are skipped) and registers its module-level `plugin_info` when that is a `PluginInfo`. A plugin whose name is already registered is skipped. `registry.load()` later imports `entry_point` by module name, so the directory must also be importable (on `sys.path`).

## Plugin Registry

The `PluginRegistry` is the central hub for managing plugins:

```python
from delegatron.plugins import PluginRegistry, PluginType

registry = PluginRegistry()

# Discover from installed packages (add search_paths=[...] for local dirs)
registry.discover()

# List all registered plugins
all_plugins = registry.list()

# Filter by type
providers = registry.list(plugin_type=PluginType.PROVIDER)

# Load a plugin class
MyProvider = registry.load("my-custom-provider")

# Manual registration (PluginError if the name is already registered)
registry.register(plugin_info)

# Unregister (KeyError if the name is not registered)
registry.unregister("my-custom-provider")
```

## Lifecycle Hooks

The `HookManager` lets you run callbacks at key pipeline events. The event names are fixed (`plugins.HOOK_EVENTS`; an unknown name raises `ValueError`), but the pipeline does not emit them yet: the triggers and kwargs below are the intended contract for code that calls `hooks.emit(event, **kwargs)`.

### Available Hook Events

| Event | Triggered When | Kwargs |
|-------|----------------|--------|
| `before_plan` | Before architect is invoked | `request`, `config` |
| `after_plan` | After architect produces WorkPlan | `plan`, `config` |
| `before_implement` | Before implementer is invoked | `task`, `iteration` |
| `after_implement` | After implementer produces PatchSet | `patch`, `iteration` |
| `before_review` | Before reviewer is invoked | `diff`, `test_report` |
| `after_review` | After reviewer produces ReviewReport | `review`, `iteration` |
| `on_approve` | Pipeline run approved | `run_id`, `summary` |
| `on_reject` | Pipeline plan rejected (interactive) | `run_id`, `reason` |
| `on_error` | Pipeline error | `run_id`, `error` |

### Registering Hooks

```python
from delegatron.plugins import HookManager

hooks = HookManager()

def notify_slack(run_id: str, summary: str, **kwargs) -> None:
    """Post a message to Slack when a run completes."""
    requests.post(SLACK_WEBHOOK, json={
        "text": f"Delegatron run {run_id} approved: {summary}"
    })

hooks.register_hook("on_approve", notify_slack)

def log_errors(run_id: str, error: str, **kwargs) -> None:
    """Log pipeline errors to an external monitoring system."""
    logger.error(f"Pipeline {run_id} failed: {error}")

hooks.register_hook("on_error", log_errors)
```

Hook callbacks receive keyword arguments. `emit()` logs an exception raised by a callback and still runs the remaining callbacks — hooks are advisory, never blocking.

### Multiple Callbacks

Multiple callbacks can be registered for the same event. They execute in registration order:

```python
hooks.register_hook("after_plan", validate_plan_size)
hooks.register_hook("after_plan", notify_team)
hooks.register_hook("after_plan", log_to_audit)
```

## Example: Custom Agent Plugin

```python
# my_agents/security_auditor.py
from delegatron.agents.base import BaseAgent
from delegatron.plugins import PluginInfo, PluginType
from pydantic import BaseModel

class SecurityReport(BaseModel):
    vulnerabilities: list[dict]
    risk_score: float
    recommendation: str

class SecurityAuditorAgent(BaseAgent):
    """Agent that performs security-focused code analysis."""

    def _build_user_message(self, input_data: dict) -> str:
        return f"Audit this code for security vulnerabilities:\n{input_data['diff']}"

    def _get_output_schema(self) -> dict:
        return SecurityReport.model_json_schema()

    def _get_output_model(self) -> type[BaseModel]:
        return SecurityReport

plugin_info = PluginInfo(
    name="security-auditor",
    version="1.0.0",
    plugin_type=PluginType.AGENT,
    entry_point="my_agents.security_auditor.SecurityAuditorAgent",
    description="Security-focused code analysis agent",
)
```

## Example: Custom Sandbox Plugin

```python
# my_sandboxes/k8s_sandbox.py
from delegatron.sandbox.base import BaseSandbox
from delegatron.plugins import PluginInfo, PluginType

class KubernetesSandbox(BaseSandbox):
    """Run tests in ephemeral Kubernetes pods."""

    async def apply_and_test(self, patch: dict) -> dict:
        # Create pod, apply patch, run tests, return TestReport
        ...

    def cleanup(self) -> None:
        # Optional: release pods/volumes when the run ends (BaseSandbox's is a no-op)
        ...

plugin_info = PluginInfo(
    name="k8s-sandbox",
    version="1.0.0",
    plugin_type=PluginType.SANDBOX,
    entry_point="my_sandboxes.k8s_sandbox.KubernetesSandbox",
)
```
