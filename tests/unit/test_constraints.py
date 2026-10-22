"""Tests for project-level constraints."""

import json
import tomllib
from pathlib import Path

import pytest

from delegatron.constraints import (
    ConstraintConfigError,
    Constraints,
    _matches_protected,
    merge_constraints,
    validate_patch,
)
from delegatron.security.patch_lines import added_lines

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_violations_when_no_constraints():
    patch = {"diff_unified": "--- a/foo.py\n+++ b/foo.py\n+print('hi')"}
    constraints = Constraints()
    violations = validate_patch(patch, constraints)
    assert violations == []


def test_no_modify_blocks_protected_files():
    patch = {"diff_unified": "--- a/LICENSE\n+++ b/LICENSE\n+modified"}
    constraints = Constraints(no_modify=["LICENSE"])
    violations = validate_patch(patch, constraints)
    assert len(violations) == 1
    assert violations[0].rule == "no_modify"
    assert violations[0].file == "LICENSE"


def test_no_modify_glob_pattern():
    patch = {"diff_unified": "--- a/.env\n+++ b/.env\n+SECRET=bad"}
    constraints = Constraints(no_modify=[".*", "*.env"])
    violations = validate_patch(patch, constraints)
    # one violation per file, even when several patterns match it
    assert [(v.rule, v.file) for v in violations] == [("no_modify", ".env")]


def test_forbidden_pattern_detected():
    patch = {
        "files": {"src/main.py": "import os\nos.system('rm -rf /')"},
    }
    constraints = Constraints(
        forbidden_patterns={"no_os_system": r"re:os\.system\("},
    )
    violations = validate_patch(patch, constraints)
    assert len(violations) == 1
    assert violations[0].rule == "forbidden_pattern"
    assert violations[0].line == 2


def test_forbidden_pattern_not_triggered():
    patch = {
        "files": {"src/main.py": "import subprocess\nsubprocess.run(['ls'])"},
    }
    constraints = Constraints(
        forbidden_patterns={"no_os_system": r"re:os\.system\("},
    )
    violations = validate_patch(patch, constraints)
    assert violations == []


def test_required_pattern_missing():
    patch = {
        "files": {"src/main.py": "def hello():\n    pass"},
    }
    constraints = Constraints(
        required_patterns={"type_hints": r"re:def \w+\(.*:.*\)"},
    )
    violations = validate_patch(patch, constraints)
    assert len(violations) == 1
    assert violations[0].rule == "required_pattern"


def test_required_pattern_present():
    patch = {
        "files": {"src/main.py": "def hello(name: str) -> str:\n    return name"},
    }
    constraints = Constraints(
        required_patterns={"type_hints": r"re:def \w+\(.*:.*\)"},
    )
    violations = validate_patch(patch, constraints)
    assert violations == []


def test_constraints_from_dict():
    data = {
        "no_modify": ["LICENSE", "*.lock"],
        "forbidden_patterns": {"no_eval": r"re:\beval\("},
        "required_patterns": {"docstring": '"""'},
    }
    c = Constraints.from_dict(data)
    assert c.no_modify == ["LICENSE", "*.lock"]
    assert "no_eval" in c.forbidden_patterns
    assert "docstring" in c.required_patterns


# ---------------------------------------------------------------------------
# Literal patterns by default, regex opt-in (#114)
# ---------------------------------------------------------------------------


def _forbidden_hits(patterns: list[str], code: str) -> list[str]:
    constraints = Constraints.from_dict({"forbidden_patterns": patterns})
    return [v.detail for v in validate_patch({"files": {"main.py": code}}, constraints)]


class TestPatternSemantics:
    def test_literal_calls_are_flagged(self):
        hits = _forbidden_hits(["eval(", "exec(", "os.system("], "result = eval(user_input)\n")
        assert hits == ["Forbidden pattern 'eval(' found: eval("]

    def test_import_star_is_literal(self):
        assert _forbidden_hits(["import *"], "from x import *\n")
        assert _forbidden_hits(["import *"], "import os\n") == []

    def test_print_parens_is_literal(self):
        assert _forbidden_hits(["print()"], "print()\n")
        assert _forbidden_hits(["print()"], "print('hi')\n") == []

    def test_regex_prefix(self):
        assert _forbidden_hits([r"re:os\.system\("], "os.system('ls')\n")
        assert _forbidden_hits([r"re:os\.system\("], "os.systemd()\n") == []

    @pytest.mark.parametrize("entry", ["re:eval(", "re:"])
    def test_invalid_regex_raises_at_construction(self, entry: str):
        with pytest.raises(ConstraintConfigError):
            Constraints.from_dict({"forbidden_patterns": [entry]})

    @pytest.mark.parametrize(
        "data",
        [
            {"no_modify": ".env"},
            {"forbidden_patterns": "eval("},
            {"required_patterns": {"x": 1}},
            {"guidelines": [1]},
        ],
    )
    def test_malformed_constraints_raise(self, data: dict):
        with pytest.raises(ConstraintConfigError):
            Constraints.from_dict(data)

    def test_blank_entries_are_ignored(self):
        c = Constraints.from_dict({"forbidden_patterns": ["", "  "], "no_modify": [""]})
        assert c.forbidden_patterns == {}
        assert c.no_modify == []


_SCOPE_PATCH = {
    "files": {
        "main.py": "def run():\n    return 1\n",
        "tests/test_main.py": "def test_run():\n    assert run() == 1\n",
        "app/__init__.py": "",
    }
}


class TestRequiredScope:
    def test_one_file_satisfies_the_rule(self):
        constraints = Constraints.from_dict({"required_patterns": ["def test_"]})
        assert validate_patch(_SCOPE_PATCH, constraints) == []

    def test_missing_pattern_gives_one_violation(self):
        constraints = Constraints.from_dict({"required_patterns": ["Copyright"]})
        violations = validate_patch(_SCOPE_PATCH, constraints)
        assert [(v.rule, v.file) for v in violations] == [("required_pattern", "")]

    def test_patch_without_source_files_is_not_checked(self):
        constraints = Constraints.from_dict({"required_patterns": ["Copyright"]})
        assert validate_patch({"files": {"README.md": "docs"}}, constraints) == []


def test_guidelines_never_produce_violations():
    constraints = Constraints.from_dict(
        {"guidelines": ["All endpoints must validate input with Pydantic models"]}
    )
    assert constraints.guidelines == ["All endpoints must validate input with Pydantic models"]
    assert validate_patch({"files": {"main.py": "x = 1\n"}}, constraints) == []


def test_issue_114_reproduction():
    patch = {
        "files": {
            "main.py": "import os\nresult = eval(user_input)\n",
            "tests/test_main.py": "def test_x():\n    assert True\n",
            "app/__init__.py": "",
            "alembic/versions/001_init.py": "revision = '001'\n",
            "config/.env": "SECRET=x\n",
        }
    }
    constraints = Constraints.from_dict(
        {
            "no_modify": ["alembic/versions/", ".env"],
            "guidelines": ["All endpoints must validate input with Pydantic models"],
            "forbidden_patterns": ["eval(", "exec(", "import *"],
        }
    )
    found = {(v.rule, v.file) for v in validate_patch(patch, constraints)}
    assert found == {
        ("forbidden_pattern", "main.py"),
        ("no_modify", "alembic/versions/001_init.py"),
        ("no_modify", "config/.env"),
    }


# ---------------------------------------------------------------------------
# no_modify matching
# ---------------------------------------------------------------------------


class TestProtectedPaths:
    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            ("alembic/versions/", "alembic/versions/001_init.py", True),
            ("alembic/versions/", "alembic/versions/sub/x.py", True),
            ("alembic/versions/", "alembic/env.py", False),
            (".env", ".env", True),
            (".env", "./.env", True),
            (".env", "config/.env", True),
            (".env", "config\\.env", True),
            (".env", ".envrc", False),
            ("src/**/*.py", "src/a.py", True),
            ("src/**/*.py", "src/x/y.py", True),
            ("src/**/*.py", "lib/a.py", False),
            ("node_modules/", "web/node_modules/react/index.js", True),
            ("node_modules/", "node_modules", False),
            ("migrations", "app/migrations/0001.py", True),
            ("/LICENSE", "LICENSE", True),
            ("/LICENSE", "docs/LICENSE", False),
            ("*.lock", "poetry.lock", True),
            ("LICENSE", "license", False),
            ("./alembic\\versions/", "alembic/versions/a.py", True),
        ],
    )
    def test_matches(self, pattern: str, path: str, expected: bool):
        assert _matches_protected(path, pattern) is expected

    def test_deleting_a_protected_file_is_a_violation(self):
        constraints = Constraints(no_modify=[".env"])
        violations = validate_patch({"files": {}, "delete_files": ["config/.env"]}, constraints)
        assert [(v.rule, v.file) for v in violations] == [("no_modify", "config/.env")]

    def test_diff_deleting_a_protected_file_is_a_violation(self):
        diff = "--- a/LICENSE\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-MIT License\n-text\n"
        violations = validate_patch({"diff_unified": diff}, Constraints(no_modify=["LICENSE"]))
        assert [(v.rule, v.file) for v in violations] == [("no_modify", "LICENSE")]

    def test_diff_creating_a_file_names_no_source(self):
        diff = "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
        assert validate_patch({"diff_unified": diff}, Constraints(no_modify=["LICENSE"])) == []

    def test_removed_line_starting_with_dashes_is_not_a_header(self):
        # "--- a/LICENSE" here is a removed line ("-- a/LICENSE"), not a file header
        diff = "--- a/notes.md\n+++ b/notes.md\n@@ -1,2 +1 @@\n--- a/LICENSE\n keep\n"
        assert validate_patch({"diff_unified": diff}, Constraints(no_modify=["LICENSE"])) == []


# ---------------------------------------------------------------------------
# Only the lines a patch adds are checked (#115)
# ---------------------------------------------------------------------------


class TestAddedLinesOnly:
    def test_existing_forbidden_line_is_not_reported(self, tmp_path: Path):
        (tmp_path / "config.py").write_text("# TODO: tidy\nA = 1\n", encoding="utf-8")
        patch = {"files": {"config.py": "# TODO: tidy\nA = 1\nB = 2\n"}}
        constraints = Constraints.from_dict({"forbidden_patterns": ["TODO"]})
        assert validate_patch(patch, constraints, added=added_lines(patch, tmp_path)) == []

    def test_added_forbidden_line_is_reported_with_its_line(self, tmp_path: Path):
        (tmp_path / "config.py").write_text("A = 1\n", encoding="utf-8")
        patch = {"files": {"config.py": "A = 1\n# TODO: later\n"}}
        constraints = Constraints.from_dict({"forbidden_patterns": ["TODO"]})
        violations = validate_patch(patch, constraints, added=added_lines(patch, tmp_path))
        assert [(v.file, v.line) for v in violations] == [("config.py", 2)]

    def test_required_pattern_uses_full_content(self, tmp_path: Path):
        (tmp_path / "main.py").write_text('"""Copyright Acme."""\n', encoding="utf-8")
        patch = {"files": {"main.py": '"""Copyright Acme."""\nx = 1\n'}}
        constraints = Constraints.from_dict({"required_patterns": ["Copyright"]})
        assert validate_patch(patch, constraints, added=added_lines(patch, tmp_path)) == []

    def test_unchanged_files_give_no_violations(self, tmp_path: Path):
        (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
        patch = {"files": {".env": "A=1\n"}}
        constraints = Constraints.from_dict(
            {"no_modify": [".env"], "required_patterns": ["x"], "forbidden_patterns": ["A"]}
        )
        assert validate_patch(patch, constraints, added=added_lines(patch, tmp_path)) == []


# ---------------------------------------------------------------------------
# merge_constraints (#97)
# ---------------------------------------------------------------------------


class TestMergeConstraints:
    def test_empty_inputs(self):
        assert merge_constraints({}, {}) == {}

    def test_list_and_table_forms_are_unioned(self):
        base = {
            "no_modify": [".env"],
            "forbidden_patterns": {"no_eval": "eval("},
            "guidelines": ["Be nice"],
        }
        extra = {
            "no_modify": [".env", "LICENSE"],
            "forbidden_patterns": ["eval(", "shell=True"],
            "required_patterns": ["def test_"],
        }
        assert merge_constraints(base, extra) == {
            "no_modify": [".env", "LICENSE"],
            "guidelines": ["Be nice"],
            "forbidden_patterns": {"no_eval": "eval(", "shell=True": "shell=True"},
            "required_patterns": {"def test_": "def test_"},
        }

    def test_extra_wins_on_name_conflict_and_other_keys_are_kept(self):
        merged = merge_constraints(
            {"forbidden_patterns": {"x": "a"}, "custom": 1},
            {"forbidden_patterns": {"x": "b"}},
        )
        assert merged == {"forbidden_patterns": {"x": "b"}, "custom": 1}

    def test_merged_result_builds_constraints(self):
        merged = merge_constraints({"no_modify": ["a"]}, {"forbidden_patterns": ["eval("]})
        assert Constraints.from_dict(merged).forbidden_patterns == {"eval(": "eval("}

    @pytest.mark.parametrize(
        "extra",
        [
            {"forbidden_patterns": ["re:eval("]},
            {"required_patterns": {"header": "re:("}},
            {"forbidden_patterns": ["re:"]},
        ],
    )
    def test_invalid_pattern_fails_at_merge(self, extra):
        """#114: a bad template entry fails when the run is configured, not at graph start."""
        with pytest.raises(ConstraintConfigError, match="re:"):
            merge_constraints({"no_modify": [".env"]}, extra)


# ---------------------------------------------------------------------------
# Shipped constraint data parses with the new parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", sorted((_REPO_ROOT / "templates").glob("*.json")), ids=lambda p: p.stem
)
def test_builtin_template_constraints_parse(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    Constraints.from_dict(data.get("constraints", {}))


def test_example_toml_constraints_parse():
    data = tomllib.loads((_REPO_ROOT / "examples" / "delegatron.toml").read_text(encoding="utf-8"))
    constraints = Constraints.from_dict(data["constraints"])
    assert constraints.no_modify
    assert constraints.forbidden_patterns
