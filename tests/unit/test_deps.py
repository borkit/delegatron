"""Tests for delegatron.security.deps — dependency auditing."""

from __future__ import annotations

from pathlib import Path

import pytest

from delegatron.security.deps import (
    _extract_imports,
    audit_imports,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_patch(diff: str) -> dict[str, str]:
    return {"diff_unified": diff}


def _make_diff(*added_lines: str) -> str:
    """Build a minimal unified diff with the given added lines."""
    header = "+++ b/app.py\n"
    body = "\n".join(f"+{line}" for line in added_lines)
    return header + body


# --------------------------------------------------------------------------- #
# stdlib detection
# --------------------------------------------------------------------------- #
class TestStdlibDetection:
    def test_os_is_stdlib(self) -> None:
        diff = _make_diff("import os")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert findings == []

    def test_json_is_stdlib(self) -> None:
        diff = _make_diff("import json")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert findings == []

    def test_pathlib_is_stdlib(self) -> None:
        diff = _make_diff("from pathlib import Path")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert findings == []

    def test_collections_submodule_is_stdlib(self) -> None:
        diff = _make_diff("from collections.abc import Mapping")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert findings == []

    def test_typing_is_stdlib(self) -> None:
        diff = _make_diff("from typing import Any")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert findings == []


# --------------------------------------------------------------------------- #
# Existing dependency detection
# --------------------------------------------------------------------------- #
class TestExistingDepDetection:
    def test_existing_dep_in_pyproject(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["requests>=2.0.0"]\n',
            encoding="utf-8",
        )
        diff = _make_diff("import requests")
        findings = audit_imports(_make_patch(diff), tmp_path)
        assert findings == []

    def test_existing_dep_with_extras(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["httpx[http2]>=0.27.0"]\n',
            encoding="utf-8",
        )
        diff = _make_diff("import httpx")
        findings = audit_imports(_make_patch(diff), tmp_path)
        assert findings == []

    def test_existing_dep_underscore_hyphen(self, tmp_path: Path) -> None:
        """Package 'python-dotenv' imports as 'dotenv'."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["python-dotenv>=1.0.0"]\n',
            encoding="utf-8",
        )
        diff = _make_diff("import dotenv")
        findings = audit_imports(_make_patch(diff), tmp_path)
        assert findings == []

    def test_existing_dep_from_requirements_txt(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("flask>=2.0\ncelery\n", encoding="utf-8")
        diff = _make_diff("import flask")
        findings = audit_imports(_make_patch(diff), tmp_path)
        assert findings == []


# --------------------------------------------------------------------------- #
# New dependency flagging
# --------------------------------------------------------------------------- #
class TestNewDepFlagging:
    def test_unknown_package_flagged(self) -> None:
        diff = _make_diff("import numpy")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert len(findings) == 1
        assert findings[0].package_name == "numpy"
        assert findings[0].severity == "warning"
        assert "numpy" in findings[0].import_line

    def test_multiple_new_deps(self) -> None:
        diff = _make_diff("import pandas", "import scipy")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        names = {f.package_name for f in findings}
        assert names == {"pandas", "scipy"}

    def test_from_import_flagged(self) -> None:
        diff = _make_diff("from boto3 import Session")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert len(findings) == 1
        assert findings[0].package_name == "boto3"

    def test_deduplication(self) -> None:
        diff = _make_diff("import numpy", "from numpy import array")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert len(findings) == 1

    def test_empty_diff_returns_empty(self) -> None:
        findings = audit_imports({"diff_unified": ""}, Path("/nonexistent"))
        assert findings == []

    def test_no_diff_key_returns_empty(self) -> None:
        findings = audit_imports({}, Path("/nonexistent"))
        assert findings == []


# --------------------------------------------------------------------------- #
# Declared dependencies (shared parser with the import validator)
# --------------------------------------------------------------------------- #
class TestDeclaredDependencies:
    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("pyproject.toml", '[project]\ndependencies = ["click>=8.0", "rich>=13.0"]\n'),
            (
                "pyproject.toml",
                "[project]\ndependencies = []\n"
                '[project.optional-dependencies]\ndev = ["click>=8.0"]\n',
            ),
            (
                "pyproject.toml",
                '[project]\ndependencies = ["click>=2.0.0; python_version < \\"3.11\\""]\n',
            ),
            ("pyproject.toml", '[dependency-groups]\ndev = ["click"]\n'),
            ("setup.cfg", "[options]\ninstall_requires =\n    click\n    sqlalchemy>=2.0\n"),
            ("requirements-dev.txt", "click\n"),
        ],
    )
    def test_declared_import_is_not_flagged(self, tmp_path: Path, name: str, content: str) -> None:
        (tmp_path / name).write_text(content, encoding="utf-8")
        assert audit_imports(_make_patch(_make_diff("import click")), tmp_path) == []

    def test_undeclared_import_is_flagged_with_location(self, tmp_path: Path) -> None:
        patch = {"files": {"app.py": "import os\nimport requests\n"}}
        findings = audit_imports(patch, tmp_path)
        assert [(f.package_name, f.file, f.line_number) for f in findings] == [
            ("requests", "app.py", 2)
        ]

    def test_requirements_added_by_the_same_patch(self, tmp_path: Path) -> None:
        patch = {"files": {"app.py": "import requests\n", "requirements.txt": "requests\n"}}
        assert audit_imports(patch, tmp_path) == []

    def test_pytest_from_dependency_groups(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[dependency-groups]\ndev = ["pytest>=8"]\n', encoding="utf-8"
        )
        patch = {"files": {"tests/test_app.py": "import pytest\n"}}
        assert audit_imports(patch, tmp_path) == []

    def test_utf16_requirements_is_parsed(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_bytes("requests\n".encode("utf-16"))
        patch = {"files": {"app.py": "import requests\n"}}
        assert audit_imports(patch, tmp_path) == []


# --------------------------------------------------------------------------- #
# Scope: files, local modules, other languages, unchanged lines
# --------------------------------------------------------------------------- #
class TestScope:
    @pytest.mark.parametrize("in_patch", [True, False])
    def test_local_module_is_not_flagged(self, tmp_path: Path, in_patch: bool) -> None:
        files = {"main.py": "from calculator import add\n"}
        if in_patch:
            files["calculator.py"] = "def add(a, b):\n    return a + b\n"
        else:
            (tmp_path / "calculator.py").write_text("def add(a, b): ...\n", encoding="utf-8")
        assert audit_imports({"files": files}, tmp_path) == []

    def test_non_python_files_are_ignored(self, tmp_path: Path) -> None:
        files = {
            "App.java": "import java.util.List;\n",
            "app.ts": "import React from 'react'\n",
            "setup.ps1": "import os\nImport-Module Foo\n",
        }
        assert audit_imports({"files": files}, tmp_path) == []

    def test_existing_import_lines_are_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
        patch = {"files": {"app.py": "import requests\nx = 1\n"}}
        assert audit_imports(patch, tmp_path) == []

    def test_unchanged_file_gives_no_findings(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
        assert audit_imports({"files": {"app.py": "import requests\n"}}, tmp_path) == []

    def test_relative_imports_are_skipped(self, tmp_path: Path) -> None:
        patch = {"files": {"pkg/mod.py": "from . import sibling\nfrom .models import X\n"}}
        assert audit_imports(patch, tmp_path) == []


# --------------------------------------------------------------------------- #
# Import extraction
# --------------------------------------------------------------------------- #
class TestExtractImports:
    def test_simple_import(self) -> None:
        results = _extract_imports(["import os"])
        assert results == [("os", "import os")]

    def test_from_import(self) -> None:
        results = _extract_imports(["from pathlib import Path"])
        assert results == [("pathlib", "from pathlib import Path")]

    def test_dotted_import(self) -> None:
        results = _extract_imports(["import xml.etree.ElementTree"])
        assert results == [("xml", "import xml.etree.ElementTree")]

    def test_non_import_lines_skipped(self) -> None:
        results = _extract_imports(["x = 1", "# import os", "print('hello')"])
        assert results == []

    @pytest.mark.parametrize(
        ("line", "modules"),
        [
            ("import os, numpy", ["os", "numpy"]),
            ("import numpy as np, pandas as pd", ["numpy", "pandas"]),
            ("import os.path as osp,yaml", ["os", "yaml"]),
            ("import requests as r  # http", ["requests"]),
        ],
    )
    def test_every_module_of_a_comma_import(self, line: str, modules: list[str]) -> None:
        assert _extract_imports([line]) == [(m, line.strip()) for m in modules]


class TestCommaSeparatedImports:
    def test_third_party_after_stdlib_is_flagged(self) -> None:
        findings = audit_imports(_make_patch(_make_diff("import os, numpy")), Path("/nonexistent"))
        assert [(f.package_name, f.import_line) for f in findings] == [
            ("numpy", "import os, numpy")
        ]

    def test_aliased_modules_are_flagged(self) -> None:
        diff = _make_diff("import numpy as np, pandas as pd")
        findings = audit_imports(_make_patch(diff), Path("/nonexistent"))
        assert [f.package_name for f in findings] == ["numpy", "pandas"]
