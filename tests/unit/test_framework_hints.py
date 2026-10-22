"""Tests for framework_hints — per-project implementer guidance.

Regression: 14B local models mix Jest and Vitest APIs (e.g. using
``jest.mock()`` in a Vitest project) because the pipeline gave them no
project-specific hints.  ``build_framework_hints`` reads package.json /
pyproject.toml and produces short, directive hints the implementer
receives in its user message.
"""

from __future__ import annotations

import codecs
import json
import re
from pathlib import Path

import pytest

from delegatron.framework_hints import build_framework_hints


def _write_pkg(tmp: Path, deps=None, dev_deps=None) -> None:
    pkg = {"name": "t", "version": "0.0.1"}
    if deps:
        pkg["dependencies"] = deps
    if dev_deps:
        pkg["devDependencies"] = dev_deps
    (tmp / "package.json").write_text(json.dumps(pkg))


class TestNodeProjects:
    def test_vitest_produces_vitest_hints(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, dev_deps={"vitest": "^2.0.0"})
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "Vitest" in joined
        assert "NOT Jest" in joined
        assert "vi.mock" in joined

    def test_jest_produces_jest_hints(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, dev_deps={"jest": "^29.0.0"})
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "Jest" in joined
        assert "NOT Vitest" in joined
        assert "jest.mock" in joined

    def test_vitest_with_jest_dom_hints_about_globals(self, tmp_path: Path) -> None:
        _write_pkg(
            tmp_path,
            dev_deps={"vitest": "^2", "@testing-library/jest-dom": "^6"},
        )
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "jest-dom" in joined
        assert "toBeInTheDocument" in joined.lower() or "vitest.setup" in joined.lower()

    def test_missing_user_event_produces_warning(self, tmp_path: Path) -> None:
        """Vitest project WITHOUT user-event installed — hint should say
        to use fireEvent instead."""
        _write_pkg(
            tmp_path,
            dev_deps={
                "vitest": "^2",
                "@testing-library/react": "^16",
                "@testing-library/jest-dom": "^6",
            },
        )
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "user-event" in joined
        assert "fireEvent" in joined

    def test_user_event_installed_no_warning(self, tmp_path: Path) -> None:
        _write_pkg(
            tmp_path,
            dev_deps={
                "vitest": "^2",
                "@testing-library/react": "^16",
                "@testing-library/user-event": "^14",
            },
        )
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        # The "user-event NOT installed" warning should NOT appear.
        assert "`@testing-library/user-event` is NOT installed" not in joined

    def test_available_deps_listed(self, tmp_path: Path) -> None:
        _write_pkg(
            tmp_path,
            dev_deps={
                "vitest": "^2",
                "@testing-library/react": "^16",
                "react": "^18",
            },
        )
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "AVAILABLE DEPENDENCIES" in joined
        assert "react" in joined
        assert "@testing-library/react" in joined

    def test_no_test_framework_no_framework_hint(self, tmp_path: Path) -> None:
        """A pure production project with no test framework shouldn't
        get a framework-specific hint."""
        _write_pkg(tmp_path, deps={"express": "^4"})
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "Vitest" not in joined
        assert "Jest" not in joined

    def test_malformed_package_json_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{ not valid json")
        assert build_framework_hints(tmp_path) == []


class TestPythonProjects:
    def test_pytest_in_pyproject_hints_use_pytest_style(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="t"\n[project.optional-dependencies]\ntest=["pytest"]\n'
        )
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "pytest" in joined.lower()
        assert "unittest.TestCase" in joined or "def test_" in joined

    def test_pytest_in_requirements_also_caught(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("pytest>=8.0\n")
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "pytest" in joined.lower()

    def test_hypothesis_detected(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("pytest\nhypothesis\n")
        hints = build_framework_hints(tmp_path)
        joined = "\n".join(hints)
        assert "hypothesis" in joined.lower()


class TestEmptyWorkspace:
    def test_no_package_json_no_python_config_returns_empty(self, tmp_path: Path) -> None:
        assert build_framework_hints(tmp_path) == []


class TestVitestUiEventHint:
    """The fireEvent hint only ever names a package that is installed (#126)."""

    def test_vue_test_utils_gets_trigger_hint(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, dev_deps={"vitest": "^2", "vue": "^3", "@vue/test-utils": "^2"})
        joined = "\n".join(build_framework_hints(tmp_path))
        assert "@testing-library/react" not in joined
        assert "trigger" in joined

    def test_vitest_only_gets_no_ui_event_hint(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, dev_deps={"vitest": "^2"})
        joined = "\n".join(build_framework_hints(tmp_path))
        assert "Vitest" in joined
        assert "fireEvent" not in joined
        assert "@testing-library/react" not in joined

    @pytest.mark.parametrize(
        "package",
        [
            "@testing-library/vue",
            "@testing-library/svelte",
            "@testing-library/preact",
            "@solidjs/testing-library",
            "@testing-library/dom",
        ],
    )
    def test_fire_event_comes_from_the_installed_package(
        self, tmp_path: Path, package: str
    ) -> None:
        _write_pkg(tmp_path, dev_deps={"vitest": "^2", package: "^1"})
        joined = "\n".join(build_framework_hints(tmp_path))
        assert f"`fireEvent` from `{package}`" in joined
        assert "@testing-library/react" not in joined

    def test_testing_library_vue_fire_event_is_awaited(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, dev_deps={"vitest": "^2", "@testing-library/vue": "^8"})
        assert "`await` every `fireEvent`" in "\n".join(build_framework_hints(tmp_path))

    def test_react_without_user_event_keeps_react_fire_event(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, dev_deps={"vitest": "^2", "@testing-library/react": "^16"})
        joined = "\n".join(build_framework_hints(tmp_path))
        assert "`fireEvent` from `@testing-library/react`" in joined

    def test_user_event_installed_gets_no_fire_event_hint(self, tmp_path: Path) -> None:
        _write_pkg(
            tmp_path,
            dev_deps={
                "vitest": "^2",
                "@testing-library/react": "^16",
                "@testing-library/user-event": "^14",
            },
        )
        joined = "\n".join(build_framework_hints(tmp_path))
        assert "NOT installed" not in joined
        assert "fireEvent" not in joined

    @pytest.mark.parametrize(
        "dev_deps",
        [
            {"vitest": "^2", "vue": "^3", "@vue/test-utils": "^2"},
            {"vitest": "^2"},
            {"vitest": "^2", "@testing-library/svelte": "^5"},
            {"vitest": "^2", "@testing-library/react": "^16"},
            {"vitest": "^2", "@testing-library/react": "^16", "@testing-library/user-event": "^14"},
            {"vitest": "^2", "@solidjs/testing-library": "^0.8", "@testing-library/dom": "^10"},
            {"vitest": "^2", "@testing-library/vue": "^8"},
            {"vitest": "^2", "@testing-library/preact": "^3"},
            {"vitest": "^2", "@testing-library/dom": "^10"},
        ],
    )
    def test_every_scoped_package_named_is_installed(
        self, tmp_path: Path, dev_deps: dict[str, str]
    ) -> None:
        _write_pkg(tmp_path, dev_deps=dev_deps)
        joined = "\n".join(build_framework_hints(tmp_path))
        import_sources = set(re.findall(r"from `(@[\w.-]+/[\w.-]+)`", joined))
        assert import_sources <= set(dev_deps)


class TestManifestEncodings:
    def test_package_json_with_bom_is_read(self, tmp_path: Path) -> None:
        pkg = json.dumps({"devDependencies": {"vitest": "^2"}})
        (tmp_path / "package.json").write_bytes(codecs.BOM_UTF8 + pkg.encode())
        assert "Vitest" in "\n".join(build_framework_hints(tmp_path))

    def test_utf16_requirements_are_read(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_bytes("pytest>=8.0\n".encode("utf-16"))
        assert "pytest" in "\n".join(build_framework_hints(tmp_path))

    def test_package_json_that_is_not_an_object_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("[]")
        assert build_framework_hints(tmp_path) == []
