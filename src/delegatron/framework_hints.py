"""Per-project framework hints for the implementer.

When we inject knowledge about the project's actual test framework and
installed dependencies into the implementer's user message, local 14B
models stop making confident-but-wrong guesses like:

- Using ``jest.mock()`` in a Vitest project
- Importing ``@testing-library/user-event`` when it's not in
  ``package.json.devDependencies``
- Mixing pytest and unittest idioms in the same file

Validated 2026-04-17: with these hints + a pre-wired ``vitest.setup.ts``,
qwen2.5-coder produces idiomatic Vitest + Testing Library code.  Without
them, the first patch has API-mixing bugs.

Keep hints SHORT and SPECIFIC.  Long prose gets ignored by smaller
models.  Each hint should correct ONE common mistake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delegatron.detection import read_text_lenient

# Packages that export ``fireEvent``, framework-specific ones first.
_FIRE_EVENT_PACKAGES = (
    "@testing-library/react",
    "@testing-library/vue",
    "@testing-library/svelte",
    "@testing-library/preact",
    "@solidjs/testing-library",
    "@testing-library/dom",
)


def _read_package_json(workspace_root: Path) -> dict[str, Any] | None:
    path = workspace_root / "package.json"
    if not path.is_file():
        return None
    try:
        pkg = json.loads(read_text_lenient(path))
    except json.JSONDecodeError:
        return None
    return pkg if isinstance(pkg, dict) else None


def _all_node_deps(pkg: dict[str, Any]) -> dict[str, str]:
    """Merged dependencies + devDependencies (name -> version spec)."""
    out: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            for name, ver in section.items():
                if isinstance(name, str) and isinstance(ver, str):
                    out[name] = ver
    return out


def _detect_node_test_framework(pkg: dict[str, Any]) -> str | None:
    """Return 'vitest' | 'jest' | 'mocha' | 'ava' | None."""
    deps = _all_node_deps(pkg)
    # Preference order: vitest is unambiguous, jest is most common,
    # mocha/ava are less common but worth detecting.
    for fw in ("vitest", "jest", "mocha", "ava"):
        if fw in deps:
            return fw
    return None


def _node_hints(pkg: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    deps = _all_node_deps(pkg)
    framework = _detect_node_test_framework(pkg)

    if framework == "vitest":
        hints.append(
            "TEST FRAMEWORK: This project uses **Vitest**, NOT Jest. "
            "Use `vi.mock()` / `vi.fn()` / `vi.spyOn()` — NEVER `jest.mock()` etc. "
            "Import from `vitest` (not `@jest/globals`)."
        )
        if "@testing-library/jest-dom" in deps:
            hints.append(
                "`@testing-library/jest-dom` matchers (toBeInTheDocument, "
                "toHaveTextContent, etc) are available — assume globally wired "
                "via `vitest.setup.ts`; do NOT add per-file imports unless you "
                "explicitly see none in the setup file."
            )
        ui_event_hint = _ui_event_hint(deps)
        if ui_event_hint:
            hints.append(ui_event_hint)
    elif framework == "jest":
        hints.append(
            "TEST FRAMEWORK: This project uses **Jest**, NOT Vitest. "
            "Use `jest.mock()` / `jest.fn()` — NEVER `vi.mock()` etc."
        )
        if "@testing-library/jest-dom" in deps:
            hints.append(
                "`@testing-library/jest-dom` is installed — its matchers "
                "(toBeInTheDocument etc) are available through the Jest setup."
            )
    elif framework:
        hints.append(
            f"TEST FRAMEWORK: This project uses **{framework}**. Match its API conventions."
        )

    # Dependency allowlist — list what they've got so the model doesn't
    # invent imports.
    if deps:
        # Keep this short — only show the deps the model is likely to
        # pick the wrong variant of (test libs + popular frameworks).
        relevant = [
            d
            for d in deps
            if any(
                kw in d
                for kw in (
                    "testing-library",
                    "vitest",
                    "jest",
                    "mocha",
                    "ava",
                    "react",
                    "vue",
                    "svelte",
                    "solid",
                    "enzyme",
                    "cypress",
                    "playwright",
                )
            )
        ]
        if relevant:
            hints.append(
                "AVAILABLE DEPENDENCIES (only import from these for "
                "test/UI libraries): " + ", ".join(sorted(relevant))
            )

    return hints


def _ui_event_hint(deps: dict[str, str]) -> str | None:
    """How to fire UI events in a Vitest project without ``user-event``.

    Only ever names a package that is in *deps*; None when ``user-event`` is
    installed or no installed package can fire events.
    """
    if "@testing-library/user-event" in deps:
        return None
    missing = "`@testing-library/user-event` is NOT installed. "
    package = next((p for p in _FIRE_EVENT_PACKAGES if p in deps), None)
    if package == "@testing-library/vue":
        return (
            missing + "Use `fireEvent` from `@testing-library/vue` for clicks and "
            "input events instead, and `await` every `fireEvent` call."
        )
    if package:
        return missing + f"Use `fireEvent` from `{package}` for clicks and input events instead."
    if "@vue/test-utils" in deps:
        return (
            missing + "Use `await wrapper.trigger('click')` / `await wrapper.setValue(...)` "
            "on the `mount()` wrapper from `@vue/test-utils` for clicks and input events."
        )
    return None


def _python_hints(workspace_root: Path) -> list[str]:
    """Detect pytest vs unittest bias + available test libs from
    ``pyproject.toml`` / ``requirements.txt``."""
    hints: list[str] = []
    deps_text = "\n".join(
        read_text_lenient(workspace_root / name)
        for name in ("pyproject.toml", "requirements.txt")
        if (workspace_root / name).is_file()
    )
    if not deps_text:
        return hints

    lower = deps_text.lower()
    if "pytest" in lower:
        hints.append(
            "TEST FRAMEWORK: pytest is installed. Use plain `def test_*()` "
            "functions with `assert` statements — NOT unittest.TestCase "
            "unless the existing code already uses it."
        )
    if "hypothesis" in lower:
        hints.append("`hypothesis` is available for property-based testing.")

    return hints


def build_framework_hints(workspace_root: Path) -> list[str]:
    """Return a list of hint strings to inject into the implementer prompt.

    Empty list when no recognizable project config exists.  Caller is
    responsible for formatting them into the user message.
    """
    hints: list[str] = []

    pkg = _read_package_json(workspace_root)
    if pkg is not None:
        hints.extend(_node_hints(pkg))

    hints.extend(_python_hints(workspace_root))

    return hints
