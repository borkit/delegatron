"""Tests for the run templates system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from delegatron.constraints import (
    ConstraintConfigError,
    Constraints,
    merge_constraints,
    validate_patch,
)
from delegatron.templates import RunTemplate, TemplateStore, _find_builtin_dir

# ---------------------------------------------------------------------------
# RunTemplate unit tests
# ---------------------------------------------------------------------------


class TestRunTemplate:
    def test_defaults(self) -> None:
        t = RunTemplate(name="basic")
        assert t.name == "basic"
        assert t.description == ""
        assert t.request_pattern == ""
        assert t.preset is None
        assert t.overrides == {}
        assert t.constraints == {}
        assert t.variables == {}

    def test_to_dict_round_trip(self) -> None:
        t = RunTemplate(
            name="full",
            description="A full template",
            request_pattern="Do {thing} in {lang}",
            preset="budget",
            overrides={"max_iterations": 3},
            constraints={"no_external_deps": True},
            variables={"thing": "refactor", "lang": "Python"},
        )
        d = t.to_dict()
        restored = RunTemplate.from_dict(d)
        assert restored.name == t.name
        assert restored.description == t.description
        assert restored.request_pattern == t.request_pattern
        assert restored.preset == t.preset
        assert restored.overrides == t.overrides
        assert restored.constraints == t.constraints
        assert restored.variables == t.variables

    def test_render_with_defaults(self) -> None:
        t = RunTemplate(
            name="greet",
            request_pattern="Hello {name}, welcome to {place}",
            variables={"name": "Alice", "place": "Wonderland"},
        )
        assert t.render() == "Hello Alice, welcome to Wonderland"

    def test_render_override_defaults(self) -> None:
        t = RunTemplate(
            name="greet",
            request_pattern="Hello {name}",
            variables={"name": "Alice"},
        )
        assert t.render({"name": "Bob"}) == "Hello Bob"

    def test_render_missing_variable_left_as_placeholder(self) -> None:
        """safe_substitute leaves missing variables as-is instead of raising."""
        t = RunTemplate(
            name="incomplete",
            request_pattern="Fix {module} in {repo}",
            variables={"module": "auth"},
        )
        result = t.render()
        assert "auth" in result
        assert "${repo}" in result

    def test_render_no_placeholders(self) -> None:
        t = RunTemplate(name="static", request_pattern="Add unit tests")
        assert t.render() == "Add unit tests"

    def test_render_extra_variables_ignored(self) -> None:
        t = RunTemplate(name="simple", request_pattern="Do {task}")
        result = t.render({"task": "refactor", "unused": "value"})
        assert result == "Do refactor"

    def test_render_dollar_brace_syntax_substitutes(self) -> None:
        """Regression: templates using `${var}` syntax (how all 30
        built-ins are written) were silently not substituting because
        the `{var}` → `${var}` regex was matching the `{var}` substring
        inside `${var}` and producing `$${var}`, which Template treats
        as escaped ``$`` + literal ``{var}``.  Fix adds a negative
        lookbehind on ``$`` so existing ``${var}`` is left alone."""
        t = RunTemplate(
            name="cli",
            request_pattern="Build ${tool} that ${action} with ${framework}",
            variables={"tool": "default-tool", "action": "does stuff", "framework": "click"},
        )
        result = t.render({"tool": "fibcli", "action": "prints Fib", "framework": "argparse"})
        assert result == "Build fibcli that prints Fib with argparse"
        # And with defaults:
        assert t.render() == "Build default-tool that does stuff with click"

    def test_render_mixed_brace_styles(self) -> None:
        """Patterns with BOTH `{x}` and `${y}` work — both get substituted."""
        t = RunTemplate(
            name="mixed",
            request_pattern="Use ${framework} to build {tool}",
        )
        result = t.render({"framework": "argparse", "tool": "fibcli"})
        assert result == "Use argparse to build fibcli"


# ---------------------------------------------------------------------------
# Built-in template constraints (#97, #114)
# ---------------------------------------------------------------------------


class TestBuiltinTemplateConstraints:
    def test_security_hardening_merges_over_toml_constraints(self) -> None:
        template = TemplateStore(store_dir=Path("unused")).load("security-hardening")
        merged = merge_constraints({"no_modify": [".env"]}, template.constraints)
        assert merged["no_modify"] == [".env"]
        assert "shell=True" in merged["forbidden_patterns"]

    def test_security_hardening_flags_banned_calls_but_not_literal_eval(self) -> None:
        template = TemplateStore(store_dir=Path("unused")).load("security-hardening")
        constraints = Constraints.from_dict(template.constraints)
        code = "import ast\nx = ast.literal_eval(s)\nsubprocess.run(cmd, shell=True)\n"
        violations = validate_patch({"files": {"app.py": code}}, constraints)
        assert [(v.file, v.line) for v in violations] == [("app.py", 3)]

    def test_prose_rules_are_guidelines(self) -> None:
        template = TemplateStore(store_dir=Path("unused")).load("ruleset-owasp")
        assert "required_patterns" not in template.constraints
        assert "Never hardcode passwords" in template.constraints["guidelines"]

    def test_user_template_with_invalid_regex_fails_at_merge(self, tmp_path: Path) -> None:
        """#114: `template run` merges before the pipeline starts; a bad `re:` fails there."""
        store = TemplateStore(store_dir=tmp_path)
        store.save(RunTemplate(name="broken", constraints={"forbidden_patterns": ["re:("]}))
        template = store.load("broken")
        with pytest.raises(ConstraintConfigError, match="Invalid regex"):
            merge_constraints({"no_modify": [".env"]}, template.constraints)


# ---------------------------------------------------------------------------
# TemplateStore tests
# ---------------------------------------------------------------------------


class TestTemplateStore:
    @pytest.fixture()
    def store(self, tmp_path: pytest.TempPathFactory) -> TemplateStore:  # type: ignore[type-arg]
        s = TemplateStore(tmp_path / "templates")  # type: ignore[operator]
        s._builtin_dir = None  # Isolate from built-in templates
        return s

    @pytest.fixture()
    def sample_template(self) -> RunTemplate:
        return RunTemplate(
            name="sample",
            description="A sample template",
            request_pattern="Implement {feature} for {project}",
            preset="budget",
            overrides={"max_iterations": 2, "max_cost_usd": 1.0},
            constraints={"language": "python"},
            variables={"feature": "auth", "project": "myapp"},
        )

    # -- save / load -------------------------------------------------------

    def test_save_creates_file(self, store: TemplateStore, sample_template: RunTemplate) -> None:
        path = store.save(sample_template)
        assert path.exists()
        assert path.suffix == ".json"

    def test_load_round_trip(self, store: TemplateStore, sample_template: RunTemplate) -> None:
        store.save(sample_template)
        loaded = store.load("sample")
        assert loaded.name == sample_template.name
        assert loaded.description == sample_template.description
        assert loaded.request_pattern == sample_template.request_pattern
        assert loaded.preset == sample_template.preset
        assert loaded.overrides == sample_template.overrides
        assert loaded.constraints == sample_template.constraints
        assert loaded.variables == sample_template.variables

    def test_load_missing_raises(self, store: TemplateStore) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            store.load("nonexistent")

    # -- list --------------------------------------------------------------

    def test_list_empty(self, store: TemplateStore) -> None:
        assert store.list_templates() == []

    def test_list_returns_all_sorted(self, store: TemplateStore) -> None:
        store.save(RunTemplate(name="beta", request_pattern="b"))
        store.save(RunTemplate(name="alpha", request_pattern="a"))
        store.save(RunTemplate(name="gamma", request_pattern="g"))
        templates = store.list_templates()
        assert [t.name for t in templates] == ["alpha", "beta", "gamma"]

    def test_list_alias(self, store: TemplateStore) -> None:
        """``store.list()`` is an alias for ``store.list_templates()``."""
        store.save(RunTemplate(name="one", request_pattern="1"))
        assert len(store.list()) == 1

    def test_undecodable_template_file_is_skipped(self, store: TemplateStore) -> None:
        store.save(RunTemplate(name="pokemon", request_pattern="Simulate a Pokémon deal"))
        (store.store_dir / "broken.json").write_bytes(b'{"name": "\xff"}')
        assert [t.request_pattern for t in store.list_templates()] == ["Simulate a Pokémon deal"]

    # -- delete ------------------------------------------------------------

    def test_delete_removes_file(self, store: TemplateStore, sample_template: RunTemplate) -> None:
        store.save(sample_template)
        store.delete("sample")
        with pytest.raises(FileNotFoundError):
            store.load("sample")

    def test_delete_missing_raises(self, store: TemplateStore) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            store.delete("ghost")

    # -- render (store-level) ----------------------------------------------

    def test_render_via_store(self, store: TemplateStore, sample_template: RunTemplate) -> None:
        store.save(sample_template)
        result = store.render("sample")
        assert result == "Implement auth for myapp"

    def test_render_with_overrides(
        self, store: TemplateStore, sample_template: RunTemplate
    ) -> None:
        store.save(sample_template)
        result = store.render("sample", {"feature": "payments"})
        assert result == "Implement payments for myapp"

    def test_render_missing_template_raises(self, store: TemplateStore) -> None:
        with pytest.raises(FileNotFoundError):
            store.render("nope")

    # -- duplicate save (overwrite) ----------------------------------------

    def test_save_overwrites_existing(self, store: TemplateStore) -> None:
        store.save(RunTemplate(name="dup", request_pattern="v1"))
        store.save(RunTemplate(name="dup", request_pattern="v2"))
        loaded = store.load("dup")
        assert loaded.request_pattern == "v2"

    # -- duplicate variables (caller wins) ---------------------------------

    def test_duplicate_variables_caller_wins(self) -> None:
        t = RunTemplate(
            name="dup-vars",
            request_pattern="{x} and {y}",
            variables={"x": "default_x", "y": "default_y"},
        )
        result = t.render({"x": "override_x"})
        assert result == "override_x and default_y"


class TestBuiltinTemplateLocation:
    """#123: the built-ins are found in an installed wheel and in the source tree."""

    def test_installed_layout_uses_the_package_templates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package = tmp_path / "site-packages" / "delegatron"
        (package / "templates").mkdir(parents=True)
        builtin = RunTemplate(name="rest-api", description="shipped", request_pattern="Build {x}")
        (package / "templates" / "rest-api.json").write_text(
            json.dumps(builtin.to_dict()), encoding="utf-8"
        )

        assert _find_builtin_dir(package) == package / "templates"

        monkeypatch.setattr(TemplateStore, "_builtin_dir", _find_builtin_dir(package))
        store = TemplateStore(store_dir=tmp_path / "user-templates")
        listed = store.list_templates()
        assert [(t.name, t.description) for t in listed] == [("rest-api", "shipped")]
        assert store.is_builtin("rest-api")

    def test_source_tree_uses_the_repository_templates(self) -> None:
        package = Path(__file__).resolve().parents[2] / "src" / "delegatron"

        found = _find_builtin_dir(package)

        assert found == package.parent.parent / "templates"
        assert (found / "rest-api.json").is_file()
