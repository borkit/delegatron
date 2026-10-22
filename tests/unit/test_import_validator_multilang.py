"""Tests for Python/Go/Rust/Java handlers in ``import_validator``."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from delegatron.security.import_validator import (
    ImportIssue,
    _extract_go_imports,
    _extract_java_imports,
    _extract_py_imports,
    _extract_rust_crates,
    _go_is_stdlib,
    _py_name_from_req_spec,
    _py_normalize,
    validate_patch_imports,
)

# =============================================================================
# Python
# =============================================================================


class TestPyImportExtraction:
    def test_simple_import(self) -> None:
        mods = _extract_py_imports("import os\n")
        assert tuple(mods) == (("os", False),)

    def test_dotted_import(self) -> None:
        # Only TOP-level component tracked.
        mods = _extract_py_imports("import foo.bar.baz\n")
        assert ("foo", False) in mods

    def test_from_import(self) -> None:
        mods = _extract_py_imports("from numpy import array\n")
        assert ("numpy", False) in mods

    def test_relative_imports_skipped(self) -> None:
        """`from . import x` / `from .foo import y` must never be flagged."""
        code = "from . import helpers\nfrom ..util import thing\n"
        mods = _extract_py_imports(code)
        assert mods == set()

    def test_multiple_on_same_file(self) -> None:
        code = "import os\nimport sys\nfrom collections import defaultdict\n"
        mods = {m for m, _ in _extract_py_imports(code)}
        assert mods == {"os", "sys", "collections"}

    def test_comma_separated_and_nested_imports(self) -> None:
        code = "import os, numpy.linalg\n\ndef f():\n    from pandas import DataFrame\n"
        assert {m for m, _ in _extract_py_imports(code)} == {"os", "numpy", "pandas"}

    def test_docstring_and_string_imports_ignored(self) -> None:
        code = (
            '"""Usage:\n\n    import fancylib\n    from otherlib import x\n"""\n'
            'EXAMPLE = """\nimport thirdlib\n"""\n'
            "import os\n"
        )
        assert _extract_py_imports(code) == {("os", False)}

    def test_invalid_escape_parses_quietly(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert _extract_py_imports('import re\nPAT = "\\d+"\n') == {("re", False)}

    def test_unparsable_file_falls_back_to_regex(self) -> None:
        code = "import numpy\nfrom . import sibling\ndef broken(:\n"
        assert _extract_py_imports(code) == {("numpy", False)}


class TestPyRequirementSpecParsing:
    def test_simple_name(self) -> None:
        assert _py_name_from_req_spec("numpy") == "numpy"

    def test_with_version(self) -> None:
        assert _py_name_from_req_spec("numpy>=1.20") == "numpy"
        assert _py_name_from_req_spec("pytest==7.0.0") == "pytest"

    def test_with_extras(self) -> None:
        assert _py_name_from_req_spec("pytest[test]") == "pytest"
        assert _py_name_from_req_spec("pytest[x,y]>=7.0") == "pytest"

    def test_normalize(self) -> None:
        # PyPI spec: names are case-insensitive and - == _ == .
        assert _py_normalize("PyYAML") == "pyyaml"
        assert _py_normalize("scikit_learn") == "scikit-learn"
        assert _py_normalize("my.package") == "my-package"

    def test_env_marker_stripped(self) -> None:
        assert _py_name_from_req_spec("pytest>=7; python_version >= '3.10'") == "pytest"


class TestPyValidation:
    def _patch(self, files: dict[str, str]) -> dict:
        return {"files": files}

    def test_stdlib_never_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\ndependencies=[]\n')
        issues = validate_patch_imports(
            self._patch({"a.py": "import os\nimport json\nimport collections\n"}),
            tmp_path,
        )
        assert issues == []

    def test_declared_dep_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="t"\ndependencies=["numpy>=1.20"]\n'
        )
        issues = validate_patch_imports(
            self._patch({"a.py": "import numpy\nfrom numpy import array\n"}),
            tmp_path,
        )
        assert issues == []

    def test_missing_dep_flagged_as_blocker(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\ndependencies=["pytest"]\n')
        issues = validate_patch_imports(
            self._patch({"a.py": "import requests\n"}),
            tmp_path,
        )
        assert len(issues) == 1
        assert issues[0].module == "requests"
        assert issues[0].severity == "blocker"

    def test_missing_dep_is_warning_when_no_deps_declared(self, tmp_path: Path) -> None:
        """Empty project: flag missing imports as warnings rather than
        blockers — we're less sure."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\ndependencies=[]\n')
        issues = validate_patch_imports(
            self._patch({"a.py": "import requests\n"}),
            tmp_path,
        )
        assert len(issues) == 1
        assert issues[0].severity == "warning"

    def test_install_name_alias_resolves(self, tmp_path: Path) -> None:
        """`PyYAML` is declared → `import yaml` resolves."""
        (tmp_path / "requirements.txt").write_text("PyYAML>=6.0\n")
        issues = validate_patch_imports(
            self._patch({"a.py": "import yaml\n"}),
            tmp_path,
        )
        assert issues == []

    def test_local_module_resolves(self, tmp_path: Path) -> None:
        """`mylib.py` at workspace root → `import mylib` is fine."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\ndependencies=[]\n')
        (tmp_path / "mylib.py").write_text("x = 1\n")
        issues = validate_patch_imports(
            self._patch({"a.py": "import mylib\n"}),
            tmp_path,
        )
        assert issues == []

    def test_local_package_resolves(self, tmp_path: Path) -> None:
        """A directory with __init__.py is also a valid local module."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\ndependencies=[]\n')
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        issues = validate_patch_imports(
            self._patch({"a.py": "from mypkg import something\n"}),
            tmp_path,
        )
        assert issues == []

    def test_relative_imports_never_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\ndependencies=[]\n')
        issues = validate_patch_imports(
            self._patch({"a.py": "from . import helpers\nfrom ..util import x\n"}),
            tmp_path,
        )
        assert issues == []

    def test_requirements_txt_parsed(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("numpy>=1.20\npytest==7.0\n# comment\n-e .\n")
        issues = validate_patch_imports(
            self._patch({"a.py": "import numpy\nimport pytest\n"}),
            tmp_path,
        )
        assert issues == []


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _validate(root: Path, files: dict[str, str], *, applied: bool = False) -> list[ImportIssue]:
    """Validate ``files`` as a patch, optionally writing it to disk first."""
    if applied:
        _write_tree(root, files)
    return validate_patch_imports({"files": files}, root)


# Patch set 001 of run 6f29441bbe8c (condensed): the module and its tests
# arrive together in one patch against an empty workspace.
_CARD_DECK_PATCH = {
    "card_deck.py": (
        "class Card:\n    pass\n\n\nclass Deck:\n    def shuffle(self):\n        import random\n"
    ),
    "tests/test_card_deck.py": (
        "import unittest\nfrom card_deck import Card, Deck\n\n\n"
        "class TestDeck(unittest.TestCase):\n    pass\n"
    ),
}


@pytest.mark.parametrize("applied", [False, True], ids=["before_apply", "after_apply"])
class TestPyRun6f29441bbe8cRegression:
    """Same-patch modules are local whether validated before or after apply."""

    def test_same_patch_module_empty_workspace(self, tmp_path: Path, applied: bool) -> None:
        assert _validate(tmp_path, _CARD_DECK_PATCH, applied=applied) == []

    def test_same_patch_module_with_requirements_txt(self, tmp_path: Path, applied: bool) -> None:
        """Declared deps used to turn the same false positive into a blocker."""
        (tmp_path / "requirements.txt").write_text("requests\n")
        assert _validate(tmp_path, _CARD_DECK_PATCH, applied=applied) == []

    def test_follow_up_task_module_in_same_patch(self, tmp_path: Path, applied: bool) -> None:
        """Patch set 004: simulate_deal.py plus its tests, card_deck.py already on disk."""
        _write_tree(tmp_path, _CARD_DECK_PATCH)
        patch = {
            "simulate_deal.py": "from card_deck import Deck\n",
            "tests/test_simulate_deal.py": "from simulate_deal import main\n",
        }
        assert _validate(tmp_path, patch, applied=applied) == []

    def test_same_patch_src_package(self, tmp_path: Path, applied: bool) -> None:
        patch = {
            "src/mypkg/__init__.py": "",
            "src/mypkg/core.py": "X = 1\n",
            "tests/test_core.py": "from mypkg.core import X\nimport mypkg\n",
        }
        assert _validate(tmp_path, patch, applied=applied) == []

    def test_same_patch_namespace_dir(self, tmp_path: Path, applied: bool) -> None:
        patch = {
            "utils/strings.py": "X = 1\n",
            "app.py": "from utils.strings import X\n",
        }
        assert _validate(tmp_path, patch, applied=applied) == []

    def test_same_patch_sibling_module(self, tmp_path: Path, applied: bool) -> None:
        """`python scripts/main.py` puts scripts/ on sys.path."""
        patch = {"scripts/util.py": "X = 1\n", "scripts/main.py": "import util\n"}
        assert _validate(tmp_path, patch, applied=applied) == []

    def test_same_patch_requirements_declares_dep(self, tmp_path: Path, applied: bool) -> None:
        patch = {"requirements.txt": "numpy\n", "calc.py": "import numpy\n"}
        assert _validate(tmp_path, patch, applied=applied) == []

    def test_same_patch_pyproject_overrides_disk(self, tmp_path: Path, applied: bool) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\ndependencies=["requests"]\n')
        patch = {
            "pyproject.toml": '[project]\nname="t"\ndependencies=["requests", "numpy"]\n',
            "calc.py": "import numpy\nimport requests\n",
        }
        assert _validate(tmp_path, patch, applied=applied) == []

    def test_missing_third_party_still_flagged(self, tmp_path: Path, applied: bool) -> None:
        patch = {**_CARD_DECK_PATCH, "card_deck.py": "import numpy\n\nclass Card: ...\n"}
        issues = _validate(tmp_path, patch, applied=applied)
        assert [(i.file, i.module, i.severity) for i in issues] == [
            ("card_deck.py", "numpy", "warning")
        ]

    def test_missing_third_party_blocker_with_declared_deps(
        self, tmp_path: Path, applied: bool
    ) -> None:
        (tmp_path / "requirements.txt").write_text("requests\n")
        patch = {**_CARD_DECK_PATCH, "card_deck.py": "import numpy\n\nclass Card: ...\n"}
        issues = _validate(tmp_path, patch, applied=applied)
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]


class TestPyLocalResolution:
    def test_missing_local_module_flagged_with_create_hint(self, tmp_path: Path) -> None:
        """A genuinely missing project module is still reported, with local advice."""
        issues = _validate(tmp_path, {"tests/test_card_deck.py": "from card_deck import Card\n"})
        assert [(i.module, i.severity) for i in issues] == [("card_deck", "warning")]
        assert "create `card_deck.py`" in issues[0].message

    def test_sibling_module_on_disk(self, tmp_path: Path) -> None:
        """pytest's prepend import mode puts the test file's dir on sys.path."""
        _write_tree(tmp_path, {"tests/helpers.py": "X = 1\n"})
        assert _validate(tmp_path, {"tests/test_a.py": "from helpers import X\n"}) == []

    def test_sibling_does_not_cover_other_dirs(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"tests/helpers.py": "X = 1\n"})
        issues = _validate(tmp_path, {"app.py": "import helpers\n"})
        assert [i.module for i in issues] == ["helpers"]

    def test_namespace_package_on_disk(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"utils/strings.py": "X = 1\n"})
        assert _validate(tmp_path, {"app.py": "from utils.strings import X\n"}) == []

    def test_nested_namespace_package_on_disk(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"game/engine/core.py": "X = 1\n"})
        assert _validate(tmp_path, {"app.py": "import game.engine.core\n"}) == []

    def test_src_namespace_package_on_disk(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"src/game/rules.py": "X = 1\n"})
        assert _validate(tmp_path, {"tests/test_rules.py": "from game.rules import X\n"}) == []

    def test_dir_without_python_code_is_not_a_module(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"numpy/README.md": "notes\n", "numpy/.hidden/x.py": ""})
        issues = _validate(tmp_path, {"calc.py": "import numpy\n"})
        assert [i.module for i in issues] == ["numpy"]

    def test_underscore_module_on_disk(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"_helpers.py": "X = 1\n"})
        assert _validate(tmp_path, {"app.py": "import _helpers\n"}) == []

    def test_underscore_names_never_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests\n")
        assert _validate(tmp_path, {"app.py": "import _private_impl\n"}) == []

    def test_conftest_import(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests\n")
        patch = {"tests/unit/test_a.py": "from conftest import make_deck\n"}
        assert _validate(tmp_path, patch) == []

    @pytest.mark.parametrize("deps", ["", "requests\n"], ids=["no_deps", "deps_without_pytest"])
    def test_pytest_always_available(self, tmp_path: Path, deps: str) -> None:
        if deps:
            (tmp_path / "requirements.txt").write_text(deps)
        patch = {"tests/test_x.py": "import pytest\nfrom _pytest.monkeypatch import MonkeyPatch\n"}
        assert _validate(tmp_path, patch) == []

    def test_docstring_only_import_not_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests\n")
        patch = {"a.py": '"""Example:\n\n    import fancylib\n"""\n\nX = 1\n'}
        assert _validate(tmp_path, patch) == []

    def test_patch_paths_normalized(self, tmp_path: Path) -> None:
        patch = {
            "./card_deck.py": "class Card: ...\n",
            "tests\\test_card_deck.py": "from card_deck import Card\nimport numpy\n",
        }
        issues = _validate(tmp_path, patch)
        assert [(i.file, i.module) for i in issues] == [("tests/test_card_deck.py", "numpy")]


class TestPyDeclaredDeps:
    def test_pep735_dependency_groups(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="t"\ndependencies=["requests"]\n'
            '[dependency-groups]\ntest=["hypothesis>=6"]\n'
            'dev=[{include-group="test"}, "ruff"]\n'
        )
        issues = _validate(
            tmp_path, {"tests/test_x.py": "import hypothesis\nimport ruff\nimport numpy\n"}
        )
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    def test_poetry_dependency_groups(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.poetry.dependencies]\npython='^3.11'\nrequests='*'\n"
            "[tool.poetry.group.dev.dependencies]\nhypothesis='*'\n"
        )
        issues = _validate(tmp_path, {"tests/test_x.py": "import hypothesis\nimport numpy\n"})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    def test_malformed_pyproject_tables_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            'project = "oops"\ndependency-groups = ["x"]\n[tool]\npoetry = 3\n'
        )
        issues = _validate(tmp_path, {"a.py": "import requests\n"})
        assert [(i.module, i.severity) for i in issues] == [("requests", "warning")]

    def test_setup_cfg_install_requires_and_extras(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text(
            "[options]\ninstall_requires =\n    requests>=2\n"
            "[options.extras_require]\ntest =\n    hypothesis\n",
            encoding="utf-8",
        )
        issues = _validate(tmp_path, {"a.py": "import requests\nimport hypothesis\nimport numpy\n"})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    def test_utf16_requirements_txt(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_bytes("requests\n".encode("utf-16"))
        issues = _validate(tmp_path, {"a.py": "import requests\nimport numpy\n"})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    def test_gitpython_provides_git(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("GitPython\n", encoding="utf-8")
        assert _validate(tmp_path, {"a.py": "import git\n"}) == []

    def test_ac_dependency_groups_with_include_group(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="t"\ndependencies=["requests"]\n'
            '[dependency-groups]\ntest=["hypothesis>=6", {include-group="lint"}]\n'
            'lint=["pytest-mock"]\n',
            encoding="utf-8",
        )
        code = "import hypothesis\nimport pytest_mock\nimport numpy\n"
        issues = _validate(tmp_path, {"tests/test_x.py": code})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    def test_pdm_and_uv_dev_dependencies(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="t"\ndependencies=["requests"]\n'
            '[tool.pdm.dev-dependencies]\ntest=["hypothesis>=6", "-e file:///x"]\n'
            '[tool.uv]\ndev-dependencies=["freezegun"]\n',
            encoding="utf-8",
        )
        code = "import hypothesis\nimport freezegun\nimport numpy\n"
        issues = _validate(tmp_path, {"tests/test_x.py": code})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]


class TestPyRequirementFiles:
    """#85: requirement files are discovered and ``-r`` includes are followed."""

    def test_ac_include_of_requirements_dev(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {
                "requirements.txt": "requests\n-r requirements-dev.txt\n",
                "requirements-dev.txt": "hypothesis\n",
            },
        )
        issues = _validate(tmp_path, {"a.py": "import hypothesis\nimport numpy\n"})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    @pytest.mark.parametrize(
        "include",
        [
            "-r reqs/extra.txt",
            "-rreqs/extra.txt",
            "--requirement reqs/extra.txt",
            "--requirement=reqs/extra.txt",
        ],
    )
    def test_include_with_any_name_and_spelling(self, tmp_path: Path, include: str) -> None:
        _write_tree(
            tmp_path,
            {"requirements.txt": "requests\n" + include + "\n", "reqs/extra.txt": "hypothesis\n"},
        )
        assert _validate(tmp_path, {"a.py": "import hypothesis\n"}) == []

    def test_include_is_relative_to_the_including_file(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {
                "requirements/test.txt": "-r ../base.txt\n-r common.txt\nhypothesis\n",
                "requirements/common.txt": "freezegun\n",
                "base.txt": "requests\n",
            },
        )
        code = "import requests\nimport freezegun\nimport hypothesis\n"
        assert _validate(tmp_path, {"a.py": code}) == []

    def test_root_requirement_files_are_discovered(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"requirements-test.txt": "hypothesis\n", "requirements/lint.txt": "freezegun\n"},
        )
        code = "import hypothesis\nimport freezegun\nimport numpy\n"
        issues = _validate(tmp_path, {"a.py": code})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    def test_same_patch_requirement_file_is_discovered(self, tmp_path: Path) -> None:
        patch = {"requirements-ci.txt": "hypothesis\n", "a.py": "import hypothesis\nimport numpy\n"}
        issues = _validate(tmp_path, patch)
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    def test_self_and_mutual_includes_terminate(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {
                "requirements.txt": "requests\n-r requirements.txt\n-r requirements-b.txt\n",
                "requirements-b.txt": "hypothesis\n-r requirements.txt\n",
            },
        )
        issues = _validate(tmp_path, {"a.py": "import hypothesis\nimport numpy\n"})
        assert [(i.module, i.severity) for i in issues] == [("numpy", "blocker")]

    @pytest.mark.parametrize("target", ["../outside.txt", "requirements/../../outside.txt"])
    def test_include_outside_the_workspace_is_not_read(self, tmp_path: Path, target: str) -> None:
        root = tmp_path / "project"
        _write_tree(root, {"requirements.txt": "requests\n-r " + target + "\n"})
        (tmp_path / "outside.txt").write_text("hypothesis\n", encoding="utf-8")
        issues = _validate(root, {"a.py": "import hypothesis\n"})
        assert [(i.module, i.severity) for i in issues] == [("hypothesis", "blocker")]

    def test_absolute_include_is_not_read(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("hypothesis\n", encoding="utf-8")
        root = tmp_path / "project"
        _write_tree(root, {"requirements.txt": "requests\n-r " + outside.as_posix() + "\n"})
        issues = _validate(root, {"a.py": "import hypothesis\n"})
        assert [(i.module, i.severity) for i in issues] == [("hypothesis", "blocker")]


# =============================================================================
# Go
# =============================================================================


class TestGoImportExtraction:
    def test_single_line_import(self) -> None:
        assert _extract_go_imports('import "fmt"\n') == {"fmt"}

    def test_grouped_imports(self) -> None:
        code = """
import (
    "fmt"
    "net/http"
    "github.com/foo/bar"
)
"""
        assert _extract_go_imports(code) == {"fmt", "net/http", "github.com/foo/bar"}


class TestGoStdlibHeuristic:
    def test_single_word_is_stdlib(self) -> None:
        assert _go_is_stdlib("fmt") is True
        assert _go_is_stdlib("net/http") is True
        assert _go_is_stdlib("encoding/json") is True

    def test_dotted_domain_not_stdlib(self) -> None:
        assert _go_is_stdlib("github.com/foo/bar") is False
        assert _go_is_stdlib("golang.org/x/sync") is False


class TestGoValidation:
    def test_stdlib_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/mine\ngo 1.22\n")
        issues = validate_patch_imports(
            {"files": {"a.go": 'import (\n "fmt"\n "net/http"\n)\n'}},
            tmp_path,
        )
        assert issues == []

    def test_declared_dep_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text(
            "module example.com/mine\ngo 1.22\nrequire github.com/foo/bar v1.0.0\n"
        )
        issues = validate_patch_imports(
            {"files": {"a.go": 'import "github.com/foo/bar"\n'}},
            tmp_path,
        )
        assert issues == []

    def test_subpath_of_declared_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text(
            "module example.com/mine\ngo 1.22\nrequire github.com/foo/bar v1.0.0\n"
        )
        issues = validate_patch_imports(
            {"files": {"a.go": 'import "github.com/foo/bar/sub/pkg"\n'}},
            tmp_path,
        )
        assert issues == []

    def test_own_module_path_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/mine\ngo 1.22\n")
        issues = validate_patch_imports(
            {"files": {"a.go": 'import "example.com/mine/util"\n'}},
            tmp_path,
        )
        assert issues == []

    def test_undeclared_third_party_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/mine\ngo 1.22\n")
        issues = validate_patch_imports(
            {"files": {"a.go": 'import "github.com/unknown/pkg"\n'}},
            tmp_path,
        )
        assert len(issues) == 1
        assert issues[0].module == "github.com/unknown/pkg"

    def test_no_gomod_skips(self, tmp_path: Path) -> None:
        assert (
            validate_patch_imports(
                {"files": {"a.go": 'import "github.com/unknown/pkg"\n'}},
                tmp_path,
            )
            == []
        )

    def test_same_patch_gomod_used(self, tmp_path: Path) -> None:
        patch = {
            "go.mod": "module example.com/game\ngo 1.22\nrequire github.com/foo/bar v1.0.0\n",
            "main.go": (
                'import (\n "example.com/game/cards"\n "github.com/foo/bar"\n'
                ' "github.com/unknown/pkg"\n)\n'
            ),
        }
        issues = validate_patch_imports({"files": patch}, tmp_path)
        assert [i.module for i in issues] == ["github.com/unknown/pkg"]


# =============================================================================
# Rust
# =============================================================================


class TestRustCrateExtraction:
    def test_use_statement(self) -> None:
        assert _extract_rust_crates("use std::fmt;\n") == {"std"}

    def test_multiple_uses(self) -> None:
        code = "use std::fmt;\nuse serde::Deserialize;\nuse anyhow::Result;\n"
        assert _extract_rust_crates(code) == {"std", "serde", "anyhow"}

    def test_extern_crate(self) -> None:
        assert _extract_rust_crates("extern crate serde;\n") == {"serde"}


class TestRustValidation:
    @pytest.fixture
    def simple_cargo(self, tmp_path: Path) -> Path:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "mine"\nversion = "0.1.0"\n[dependencies]\nserde = "1"\n'
        )
        return tmp_path

    def test_std_resolves(self, simple_cargo: Path) -> None:
        issues = validate_patch_imports(
            {"files": {"lib.rs": "use std::fmt;\nuse core::cmp;\n"}},
            simple_cargo,
        )
        assert issues == []

    def test_declared_resolves(self, simple_cargo: Path) -> None:
        issues = validate_patch_imports(
            {"files": {"lib.rs": "use serde::Deserialize;\n"}},
            simple_cargo,
        )
        assert issues == []

    def test_undeclared_flagged(self, simple_cargo: Path) -> None:
        issues = validate_patch_imports(
            {"files": {"lib.rs": "use tokio::runtime;\n"}},
            simple_cargo,
        )
        assert len(issues) == 1
        assert issues[0].module == "tokio"

    def test_hyphen_vs_underscore_names(self, tmp_path: Path) -> None:
        """Cargo name `my-crate` → Rust use `my_crate`.  Validator should
        normalize both to the same identifier."""
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname="t"\nversion="0.1.0"\n[dependencies]\nmy-crate = "1"\n'
        )
        issues = validate_patch_imports(
            {"files": {"lib.rs": "use my_crate::foo;\n"}},
            tmp_path,
        )
        assert issues == []

    def test_own_crate_name_resolves(self, tmp_path: Path) -> None:
        """Own crate's name should resolve when referenced from inside."""
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname="my-lib"\nversion="0.1.0"\n[dependencies]\n'
        )
        issues = validate_patch_imports(
            {"files": {"tests/it.rs": "use my_lib::api;\n"}},
            tmp_path,
        )
        assert issues == []

    def test_mod_declaration_is_local(self, simple_cargo: Path) -> None:
        """Rust 2018: `mod cards; use cards::Card;` names a module, not a crate."""
        patch = {
            "src/main.rs": "mod cards;\npub(crate) mod util;\nuse cards::Card;\nuse util::*;\n",
            "src/cards.rs": "pub struct Card;\n",
        }
        assert validate_patch_imports({"files": patch}, simple_cargo) == []

    def test_crate_relative_paths_are_local(self, simple_cargo: Path) -> None:
        code = "use crate::cards::Card;\nuse self::inner::X;\nuse super::Deck;\n"
        assert validate_patch_imports({"files": {"src/game.rs": code}}, simple_cargo) == []

    def test_local_enum_glob_is_local(self, simple_cargo: Path) -> None:
        code = "pub enum Color {\n    Red,\n}\nuse Color::*;\nfn main() {}\n"
        assert validate_patch_imports({"files": {"src/main.rs": code}}, simple_cargo) == []

    def test_unknown_crate_still_flagged_next_to_local_mods(self, simple_cargo: Path) -> None:
        code = "mod cards;\nuse cards::Card;\nuse tokio::runtime;\n"
        issues = validate_patch_imports({"files": {"src/main.rs": code}}, simple_cargo)
        assert [i.module for i in issues] == ["tokio"]

    def test_same_patch_cargo_toml_used(self, tmp_path: Path) -> None:
        patch = {
            "Cargo.toml": '[package]\nname = "game"\n[dependencies]\nrand = "0.8"\n',
            "src/main.rs": "use rand::Rng;\nuse serde::Serialize;\n",
        }
        issues = validate_patch_imports({"files": patch}, tmp_path)
        assert [i.module for i in issues] == ["serde"]

    @pytest.mark.parametrize("on_disk", [False, True], ids=["no-cards-rs", "cards-rs-on-disk"])
    @pytest.mark.parametrize(
        "code",
        [
            "mod cards;\nuse cards::Card;\n",
            "pub mod cards;\n",
            "pub(crate) mod cards;\nuse cards::Card;\n",
            "enum Color { Red }\nuse Color::*;\n",
            "use Color::{Red, Green};\n",
            "use Direction::North;\n",
            "mod types;\nuse types::Direction;\nuse Direction::*;\n",
        ],
    )
    def test_local_items_and_types_are_not_crates(
        self, simple_cargo: Path, code: str, on_disk: bool
    ) -> None:
        """#85: in-scope modules and uppercase roots (types, enum variants) are local."""
        if on_disk:
            (simple_cargo / "src").mkdir()
            (simple_cargo / "src" / "cards.rs").write_text("pub struct Card;\n", encoding="utf-8")
        assert validate_patch_imports({"files": {"src/main.rs": code}}, simple_cargo) == []

    def test_local_names_are_per_file(self, simple_cargo: Path) -> None:
        """A `mod tokio;` in one file does not hide an undeclared crate in another."""
        patch = {"src/a.rs": "mod tokio;\n", "src/b.rs": "use tokio::runtime;\n"}
        issues = validate_patch_imports({"files": patch}, simple_cargo)
        assert [(i.file, i.module) for i in issues] == [("src/b.rs", "tokio")]

    def test_similar_local_module_does_not_hide_crate(self, simple_cargo: Path) -> None:
        code = "mod tokio_helpers;\nuse tokio::runtime;\n"
        issues = validate_patch_imports({"files": {"src/main.rs": code}}, simple_cargo)
        assert [i.module for i in issues] == ["tokio"]

    def test_target_specific_dependencies(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "app"\n[dependencies]\nserde = "1"\n'
            "[target.'cfg(windows)'.dependencies]\nwinapi = \"0.3\"\n"
            "[target.'cfg(unix)'.dev-dependencies]\nmock-all = \"0.11\"\n",
            encoding="utf-8",
        )
        code = "use winapi::um;\nuse mock_all::automock;\nuse tokio::runtime;\n"
        issues = validate_patch_imports({"files": {"src/main.rs": code}}, tmp_path)
        assert [i.module for i in issues] == ["tokio"]


# =============================================================================
# Java (best-effort)
# =============================================================================


class TestJavaImportExtraction:
    def test_simple(self) -> None:
        assert _extract_java_imports("import java.util.List;\n") == {"java.util.List"}

    def test_static(self) -> None:
        assert _extract_java_imports(
            "import static org.junit.jupiter.api.Assertions.assertEquals;\n"
        ) == {"org.junit.jupiter.api.Assertions.assertEquals"}

    def test_wildcard(self) -> None:
        # `.*` is stripped by the regex — we capture the package prefix.
        assert _extract_java_imports("import java.util.*;\n") == {"java.util"}


class TestJavaValidation:
    def test_jdk_imports_never_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies><dependency><artifactId>x</artifactId>"
            "</dependency></dependencies></project>"
        )
        code = (
            "import java.util.List;\n"
            "import javax.swing.JFrame;\n"
            "import java.util.concurrent.ConcurrentHashMap;\n"
            "import org.junit.jupiter.api.Test;\n"  # proves the file was checked
        )
        issues = validate_patch_imports({"files": {"F.java": code}}, tmp_path)
        assert [iss.module for iss in issues] == ["org.junit.jupiter.api.Test"]

    def test_junit_with_artifact_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies><dependency>"
            "<artifactId>junit-jupiter</artifactId></dependency>"
            "</dependencies></project>"
        )
        issues = validate_patch_imports(
            {"files": {"F.java": "import org.junit.jupiter.api.Test;\n"}},
            tmp_path,
        )
        assert issues == []

    def test_junit_without_artifact_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies><dependency>"
            "<artifactId>commons-lang3</artifactId></dependency>"
            "</dependencies></project>"
        )
        issues = validate_patch_imports(
            {"files": {"F.java": "import org.junit.jupiter.api.Test;\n"}},
            tmp_path,
        )
        assert any(iss.module.startswith("org.junit") for iss in issues)

    def test_unknown_import_not_flagged(self, tmp_path: Path) -> None:
        """User's own packages / obscure libs can't be mapped to artifacts.
        Stay silent rather than false-positive."""
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies><dependency>"
            "<artifactId>junit-jupiter</artifactId></dependency>"
            "</dependencies></project>"
        )
        issues = validate_patch_imports(
            {"files": {"F.java": "import com.example.myapp.Thing;\n"}},
            tmp_path,
        )
        assert issues == []

    def test_no_pom_skips(self, tmp_path: Path) -> None:
        """No pom.xml → skip Java validation entirely.

        The same import is flagged once a pom without junit exists
        (``test_junit_without_artifact_flagged``)."""
        issues = validate_patch_imports(
            {"files": {"F.java": "import org.junit.jupiter.api.Test;\n"}},
            tmp_path,
        )
        assert issues == []
