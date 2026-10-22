"""Tests for import_validator — cross-check patch imports vs package.json.

Catches the common 14B-model mistake of importing a package that isn't
installed (e.g. `@testing-library/user-event` in a project that only has
`@testing-library/react`).
"""

from __future__ import annotations

import json
from pathlib import Path

from delegatron.security.import_validator import (
    _extract_js_imports,
    _is_bare_specifier,
    _js_path_aliases,
    _package_name_from_specifier,
    _strip_jsonc,
    validate_patch_imports,
)


def _write_pkg(tmp: Path, deps=None, dev_deps=None) -> None:
    pkg = {"name": "t", "version": "0.0.1"}
    if deps:
        pkg["dependencies"] = deps
    if dev_deps:
        pkg["devDependencies"] = dev_deps
    (tmp / "package.json").write_text(json.dumps(pkg))


class TestImportExtraction:
    def test_import_from(self) -> None:
        assert _extract_js_imports("import React from 'react'") == {"react"}

    def test_named_imports(self) -> None:
        code = "import { render, screen } from '@testing-library/react'\n"
        assert _extract_js_imports(code) == {"@testing-library/react"}

    def test_side_effect_import(self) -> None:
        assert _extract_js_imports("import '@testing-library/jest-dom/vitest'\n") == {
            "@testing-library/jest-dom/vitest"
        }

    def test_multiple_imports(self) -> None:
        code = """
import React from 'react'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import './Counter.css'
"""
        mods = _extract_js_imports(code)
        assert "react" in mods
        assert "@testing-library/react" in mods
        assert "@testing-library/user-event" in mods
        assert "./Counter.css" in mods

    def test_commonjs_require(self) -> None:
        assert _extract_js_imports("const fs = require('fs');\n") == {"fs"}


class TestSpecifierHelpers:
    def test_bare_specifier(self) -> None:
        assert _is_bare_specifier("react") is True
        assert _is_bare_specifier("@scope/pkg") is True

    def test_relative_not_bare(self) -> None:
        assert _is_bare_specifier("./foo") is False
        assert _is_bare_specifier("../bar") is False
        assert _is_bare_specifier("/abs/path") is False

    def test_url_not_bare(self) -> None:
        assert _is_bare_specifier("https://example.com/mod") is False

    def test_node_scheme_not_bare(self) -> None:
        assert _is_bare_specifier("node:fs") is False
        assert _is_bare_specifier("node:test") is False

    def test_path_aliases_not_bare(self) -> None:
        assert _is_bare_specifier("@/components/Card") is False
        assert _is_bare_specifier("~/utils/deck") is False
        assert _is_bare_specifier("@scope/pkg") is True

    def test_package_name_from_unscoped(self) -> None:
        assert _package_name_from_specifier("react") == "react"
        assert _package_name_from_specifier("react-dom/client") == "react-dom"

    def test_package_name_from_scoped(self) -> None:
        assert _package_name_from_specifier("@testing-library/react") == "@testing-library/react"
        assert (
            _package_name_from_specifier("@testing-library/jest-dom/vitest")
            == "@testing-library/jest-dom"
        )


class TestValidatePatchImports:
    def test_happy_path_all_imports_resolve(self, tmp_path: Path) -> None:
        _write_pkg(
            tmp_path,
            deps={"react": "^18"},
            dev_deps={"vitest": "^2", "@testing-library/react": "^16"},
        )
        patch = {
            "files": {
                "Counter.tsx": "import React from 'react'\nexport default function C(){}\n",
                "Counter.test.tsx": (
                    "import { render } from '@testing-library/react'\n"
                    "import Counter from './Counter'\n"
                ),
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert issues == []

    def test_unresolvable_package_flagged(self, tmp_path: Path) -> None:
        _write_pkg(
            tmp_path,
            dev_deps={"vitest": "^2", "@testing-library/react": "^16"},
        )
        patch = {
            "files": {
                "Counter.test.tsx": (
                    "import { render } from '@testing-library/react'\n"
                    "import userEvent from '@testing-library/user-event'\n"
                )
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert len(issues) == 1
        assert issues[0].module == "@testing-library/user-event"
        assert issues[0].severity == "blocker"
        assert "not in package.json" in issues[0].message

    def test_relative_imports_never_flagged(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={"react": "^18"})
        patch = {
            "files": {
                "a.ts": "import b from './b'\nimport c from '../c'\n",
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert issues == []

    def test_node_builtins_never_flagged(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={})
        patch = {
            "files": {
                "a.ts": "import fs from 'fs'\nimport path from 'path'\nimport 'http'\n",
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert issues == []

    def test_node_scheme_builtins_never_flagged(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={})
        code = (
            "import fs from 'node:fs'\n"
            "import { readFile } from 'node:fs/promises'\n"
            "import test from 'node:test'\n"
            "const { createRequire } = require('module')\n"
            "const hooks = require('async_hooks')\n"
        )
        assert validate_patch_imports({"files": {"deck.test.mjs": code}}, tmp_path) == []

    def test_path_aliases_never_flagged(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={"react": "^18"})
        code = "import Card from '@/components/Card'\nimport { shuffle } from '~/lib/deck'\n"
        assert validate_patch_imports({"files": {"src/App.tsx": code}}, tmp_path) == []

    def test_missing_package_flagged_next_to_builtins_and_aliases(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={"react": "^18"})
        code = (
            "import fs from 'node:fs'\n"
            "import Card from '@/components/Card'\n"
            "import _ from 'lodash'\n"
        )
        issues = validate_patch_imports({"files": {"src/a.ts": code}}, tmp_path)
        assert [(i.module, i.severity) for i in issues] == [("lodash", "blocker")]

    def test_same_patch_package_json_used(self, tmp_path: Path) -> None:
        """Deps the patch itself adds count before the patch is applied."""
        _write_pkg(tmp_path, deps={"react": "^18"})
        patch = {
            "package.json": json.dumps({"dependencies": {"react": "^18", "lodash": "^4"}}),
            "src/a.js": "import _ from 'lodash'\n",
        }
        assert validate_patch_imports({"files": patch}, tmp_path) == []

    def test_subpath_imports_resolve_if_root_installed(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={"react-dom": "^18"})
        patch = {
            "files": {
                "a.ts": "import { createRoot } from 'react-dom/client'\n",
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert issues == []

    def test_scoped_subpath_imports(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, dev_deps={"@testing-library/jest-dom": "^6"})
        patch = {
            "files": {
                "a.ts": "import '@testing-library/jest-dom/vitest'\n",
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert issues == []

    def test_no_package_json_noop(self, tmp_path: Path) -> None:
        """Python projects or pre-init repos — no package.json means
        we can't validate JS imports.  Should not crash, should not flag."""
        patch = {
            "files": {
                "a.ts": "import userEvent from '@testing-library/user-event'\n",
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert issues == []

    def test_non_code_files_ignored(self, tmp_path: Path) -> None:
        """Non-code files (markdown, JSON, etc.) must never be scanned."""
        _write_pkg(tmp_path, deps={})
        patch = {
            "files": {
                "README.md": "# Notes\nimport 'something' from somewhere\n",
                "config.json": '{"import": "x"}',
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert issues == []

    def test_python_files_validated_when_package_json_exists(self, tmp_path: Path) -> None:
        """A polyglot project (package.json + .py files) validates both."""
        _write_pkg(tmp_path, deps={})
        patch = {
            "files": {
                "a.py": "import os\nimport some_nonexistent_package\n",
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        # Python gets a warning (no declared Python deps to compare against).
        assert [(iss.file, iss.module, iss.severity) for iss in issues] == [
            ("a.py", "some_nonexistent_package", "warning")
        ]

    def test_multiple_files_multiple_issues(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={"react": "^18"})
        patch = {
            "files": {
                "a.ts": "import x from 'missing-one'\n",
                "b.ts": "import y from 'missing-two'\n",
            }
        }
        issues = validate_patch_imports(patch, tmp_path)
        assert len(issues) == 2
        modules = {iss.module for iss in issues}
        assert modules == {"missing-one", "missing-two"}

    def test_empty_patch_noop(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={})
        assert validate_patch_imports({}, tmp_path) == []
        assert validate_patch_imports({"files": {}}, tmp_path) == []


# The #85 acceptance tsconfig: comments, a trailing comma, and a "@/*" key
# that contains "/*" (a regex comment stripper would break on it).
_AC_TSCONFIG = """{
  // compiler options
  "compilerOptions": {
    /* path aliases */
    "baseUrl": ".",
    "paths": {"@/*": ["./src/*"], "@app/*": ["src/app/*"], "$lib/*": ["src/lib/*"],},
  },
}
"""


class TestPathAliases:
    """#85: tsconfig/jsconfig ``compilerOptions.paths`` aliases are not packages."""

    def test_strip_jsonc_keeps_strings(self) -> None:
        text = '{"a": "x // y", "b/*": "/* z */", // c\n "d": [1, 2,], /* e */}'
        assert json.loads(_strip_jsonc(text)) == {"a": "x // y", "b/*": "/* z */", "d": [1, 2]}

    def test_aliases_from_tsconfig(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text(_AC_TSCONFIG, encoding="utf-8")
        exact, prefixes = _js_path_aliases(tmp_path, {})
        assert exact == frozenset()
        assert prefixes == ("$lib/", "@/", "@app/")

    def test_exact_alias_and_jsconfig(self, tmp_path: Path) -> None:
        (tmp_path / "jsconfig.json").write_text(
            '{"compilerOptions": {"paths": {"config": ["src/config.js"], "*": ["types/*"]}}}',
            encoding="utf-8",
        )
        assert _js_path_aliases(tmp_path, {}) == (frozenset({"config"}), ())

    def test_ac_tsconfig_aliases_resolve_but_lookalike_package_is_flagged(
        self, tmp_path: Path
    ) -> None:
        _write_pkg(tmp_path, deps={"react": "^18"})
        (tmp_path / "tsconfig.json").write_text(_AC_TSCONFIG, encoding="utf-8")
        code = (
            "import { api } from '@app/foo'\n"
            "import { x } from '$lib/x'\n"
            "import Card from '@/components/Card'\n"
            "import { y } from '@apple/x'\n"
        )
        issues = validate_patch_imports({"files": {"src/a.ts": code}}, tmp_path)
        assert [(i.module, i.severity) for i in issues] == [("@apple/x", "blocker")]

    def test_same_patch_tsconfig_is_used(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={})
        patch = {"tsconfig.json": _AC_TSCONFIG, "src/a.ts": "import { x } from '$lib/x'\n"}
        assert validate_patch_imports({"files": patch}, tmp_path) == []

    def test_malformed_tsconfig_is_ignored(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={})
        (tmp_path / "tsconfig.json").write_text(
            '{"compilerOptions": {"paths": {"@app/*": [', encoding="utf-8"
        )
        assert _js_path_aliases(tmp_path, {}) == (frozenset(), ())
        issues = validate_patch_imports({"files": {"a.ts": "import a from '@app/foo'\n"}}, tmp_path)
        assert [(i.module, i.severity) for i in issues] == [("@app/foo", "blocker")]

    def test_missing_packages_still_flagged(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={"@testing-library/react": "^14"})
        (tmp_path / "tsconfig.json").write_text(_AC_TSCONFIG, encoding="utf-8")
        code = "import _ from 'lodash'\nimport userEvent from '@testing-library/user-event'\n"
        issues = validate_patch_imports({"files": {"a.test.tsx": code}}, tmp_path)
        assert sorted((i.module, i.severity) for i in issues) == [
            ("@testing-library/user-event", "blocker"),
            ("lodash", "blocker"),
        ]

    def test_deprecated_builtin_aliases(self, tmp_path: Path) -> None:
        _write_pkg(tmp_path, deps={})
        code = "const c = require('constants')\nconst s = require('sys')\n"
        assert validate_patch_imports({"files": {"a.js": code}}, tmp_path) == []
