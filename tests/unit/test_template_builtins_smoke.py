"""Smoke test: every shipped built-in template renders cleanly.

Regression guard for the 2026-04-17 bug where ``RunTemplate.render``
silently didn't substitute ``${var}`` placeholders.  That failure mode
was invisible — the CLI happily sent literal ``${tool_name}`` to the
architect, which then produced plans for a project called
``${tool_name}``.

This test iterates every JSON in ``templates/``, renders it with the
template's OWN default variables (which the UI uses to pre-fill the
``--var`` flags), and asserts:

1. Every ``${foo}`` placeholder in the pattern corresponds to a
   variable declared in ``variables``.  (No "required var missing
   from defaults" footguns.)
2. After rendering with defaults, no unsubstituted ``${...}`` remains
   in the output.
3. The rendered pattern is a non-empty string at least as long as the
   template name (i.e. defaults aren't all empty strings).

These checks are cheap (milliseconds), catch real bugs, and run in
parallel to anything that's using the GPU.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from delegatron.templates import RunTemplate

# Templates ship in the repo root under templates/.
_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "templates"


def _builtin_template_paths() -> list[Path]:
    """All *.json files shipped in templates/."""
    return sorted(_BUILTIN_DIR.glob("*.json"))


# Generate one test-case per template so pytest shows which one failed.
@pytest.mark.parametrize(
    "tpl_path",
    _builtin_template_paths(),
    ids=lambda p: p.stem,
)
class TestBuiltinTemplate:
    def test_loads_without_error(self, tpl_path: Path) -> None:
        data = json.loads(tpl_path.read_text(encoding="utf-8"))
        RunTemplate.from_dict(data)

    def test_declared_variables_cover_placeholders(self, tpl_path: Path) -> None:
        """Every ``${var}`` / ``{var}`` placeholder in the pattern must
        have a default value under ``variables``.  Otherwise the
        template is unusable without manually passing the var."""
        data = json.loads(tpl_path.read_text(encoding="utf-8"))
        t = RunTemplate.from_dict(data)
        pattern = t.request_pattern
        # Capture both `${foo}` and standalone `{foo}` (but NOT `{}`).
        placeholders = set(
            re.findall(r"\$\{(\w+)\}", pattern) + re.findall(r"(?<!\$)\{(\w+)\}", pattern)
        )
        missing = placeholders - set(t.variables or {})
        assert missing == set(), (
            f"Template {t.name!r} has placeholders {sorted(missing)} "
            f"with no default in `variables`.  Either add defaults or "
            f"remove the placeholders."
        )

    def test_renders_cleanly_with_defaults(self, tpl_path: Path) -> None:
        """No ``${...}`` or ``{...}`` must remain after rendering with
        the template's own defaults.  Regression for the silent-no-op
        render bug fixed 2026-04-17."""
        data = json.loads(tpl_path.read_text(encoding="utf-8"))
        t = RunTemplate.from_dict(data)
        rendered = t.render()
        # `${...}` remnant (the 2026-04-17 bug signature).
        assert "${" not in rendered, (
            f"Template {t.name!r} left a `${{...}}` placeholder after "
            f"rendering with defaults: {rendered[:200]!r}"
        )
        # Bare `{var}` remnant — Python string.Template doesn't natively
        # handle this, but our render() upgrades it.  Still guard.
        unsubbed = re.findall(r"(?<!\$)\{(\w+)\}", rendered)
        assert not unsubbed, (
            f"Template {t.name!r} left bare `{{{unsubbed[0]}}}` placeholder: {rendered[:200]!r}"
        )
        # Sanity: rendered string is non-trivially long.
        assert len(rendered) >= max(len(t.name), 20), (
            f"Template {t.name!r} rendered to suspiciously short string: {rendered!r}"
        )

    def test_name_matches_filename(self, tpl_path: Path) -> None:
        """Guard against typos: template.name should match the filename
        stem (case-sensitive)."""
        data = json.loads(tpl_path.read_text(encoding="utf-8"))
        t = RunTemplate.from_dict(data)
        assert t.name == tpl_path.stem, (
            f"Template filename {tpl_path.stem!r} doesn't match its declared name {t.name!r}."
        )

    def test_description_is_human_readable(self, tpl_path: Path) -> None:
        """Not a hard requirement, but every built-in should have at
        least a brief description so the /templates dashboard can show
        something useful."""
        data = json.loads(tpl_path.read_text(encoding="utf-8"))
        t = RunTemplate.from_dict(data)
        assert t.description and len(t.description) >= 10, (
            f"Template {t.name!r} has a missing or trivial description."
        )


def test_at_least_20_builtins_ship() -> None:
    """Canary — if someone accidentally drops templates/, catch it."""
    paths = _builtin_template_paths()
    assert len(paths) >= 20, (
        f"Expected ≥20 built-in templates; found {len(paths)} in {_BUILTIN_DIR}."
    )
