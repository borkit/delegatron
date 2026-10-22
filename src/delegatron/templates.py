"""Run templates for reusable pipeline configurations."""

from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _sanitize_name(name: str) -> str:
    """Sanitize a template name to a safe filename stem."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


@dataclass
class RunTemplate:
    """A reusable run template with optional placeholders."""

    name: str
    description: str = ""
    request_pattern: str = ""
    preset: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)

    def render(self, variables: dict[str, str] | None = None) -> str:
        """Render *request_pattern* by substituting ``$placeholders``.

        Uses :class:`string.Template.safe_substitute` so that missing
        variables are left as-is rather than raising, and curly braces in
        the pattern are not misinterpreted as format specs.

        *variables* is merged on top of the template's own default variables.
        """
        merged = {**self.variables, **(variables or {})}
        # Accept either `{var}` or `${var}` in the pattern.  For the plain
        # `{var}` form, upgrade to `${var}` so ``string.Template`` picks it
        # up.  Negative lookbehind on ``$`` prevents the regex from firing
        # on existing ``${var}`` (which contains the substring ``{var}`` —
        # unguarded, the regex would produce ``$${var}`` and Template would
        # treat ``$$`` as an escaped literal ``$``, skipping substitution.)
        pattern = re.sub(r"(?<!\$)\{(\w+)\}", r"${\1}", self.request_pattern)
        return string.Template(pattern).safe_substitute(merged)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "request_pattern": self.request_pattern,
        }
        if self.preset is not None:
            data["preset"] = self.preset
        if self.overrides:
            data["overrides"] = self.overrides
        if self.constraints:
            data["constraints"] = self.constraints
        if self.variables:
            data["variables"] = self.variables
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunTemplate:
        """Deserialise from a JSON-loaded dict."""
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            request_pattern=data.get("request_pattern", ""),
            preset=data.get("preset"),
            overrides=dict(data.get("overrides", {})),
            constraints=dict(data.get("constraints", {})),
            variables=dict(data.get("variables", {})),
        )


def _find_builtin_dir(package_dir: Path) -> Path:
    """The built-in templates: ``<package>/templates`` when installed, else the repo's."""
    installed = package_dir / "templates"  # wheel force-include
    return installed if installed.is_dir() else package_dir.parent.parent / "templates"


class TemplateStore:
    """Persist :class:`RunTemplate` objects as JSON files in a directory."""

    # Built-in templates directory. Override in tests to isolate.
    _builtin_dir: Path | None = _find_builtin_dir(Path(__file__).resolve().parent)

    def __init__(self, store_dir: Path) -> None:
        self.store_dir = store_dir

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save(self, template: RunTemplate) -> Path:
        """Save a template to disk, returning the path written.

        Raises :class:`ValueError` if any variable name in the template's
        default *variables* dict is not a valid Python identifier.
        """
        for var_name in template.variables:
            if not var_name.isidentifier():
                raise ValueError(
                    f"Invalid variable name {var_name!r} in template "
                    f"'{template.name}': must be a valid identifier"
                )
        self.store_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(template.name)
        path.write_text(json.dumps(template.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path

    def load(self, name: str) -> RunTemplate:
        """Load a template by name.  Raises ``FileNotFoundError`` if missing.

        User templates (store_dir) take priority over built-ins.
        """
        # Check user templates first
        path = self._path_for(name)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return RunTemplate.from_dict(data)

        # Fall back to built-in templates
        if self._builtin_dir is not None:
            builtin_path = self._builtin_dir / f"{_sanitize_name(name)}.json"
            if builtin_path.exists():
                data = json.loads(builtin_path.read_text(encoding="utf-8"))
                return RunTemplate.from_dict(data)

        raise FileNotFoundError(f"Template '{name}' not found")

    def list_templates(self) -> list[RunTemplate]:
        """Return all templates (built-in + user), sorted by name.

        Built-in templates ship with Delegatron in the templates/ directory.
        User templates in the store_dir override built-ins with the same name.
        """
        templates_by_name: dict[str, RunTemplate] = {}

        # Load built-in templates first
        builtin_dir = self._builtin_dir
        if builtin_dir is not None and builtin_dir.exists():
            for path in sorted(builtin_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    t = RunTemplate.from_dict(data)
                    templates_by_name[t.name] = t
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
                    pass

        # User templates override built-ins
        if self.store_dir.exists():
            for path in sorted(self.store_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    t = RunTemplate.from_dict(data)
                    templates_by_name[t.name] = t
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
                    pass

        return sorted(templates_by_name.values(), key=lambda t: t.name)

    def is_builtin(self, name: str) -> bool:
        """Return True when *name* resolves to a shipped built-in template
        (not overridden by a user copy in ``store_dir``)."""
        # User file takes priority; if present, treat as non-builtin.
        if self._path_for(name).exists():
            return False
        if self._builtin_dir is None:
            return False
        return (self._builtin_dir / f"{_sanitize_name(name)}.json").exists()

    # Alias kept for CLI compatibility.
    list = list_templates

    def delete(self, name: str) -> None:
        """Delete a template.  Raises ``FileNotFoundError`` if missing."""
        path = self._path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"Template '{name}' not found at {path}")
        path.unlink()

    def render(self, name: str, variables: dict[str, str] | None = None) -> str:
        """Load a template and render its request pattern."""
        template = self.load(name)
        return template.render(variables)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _path_for(self, name: str) -> Path:
        return self.store_dir / f"{_sanitize_name(name)}.json"
