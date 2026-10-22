"""Tests for schemas/patch_set.py — PatchSet model coercion validators."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from delegatron.agents.implementer import ImplementerAgent
from delegatron.agents.providers.base import BaseProvider, ProviderResponse
from delegatron.schemas.patch_set import EditMismatchError, FileEdit, PatchSet, apply_edits


class TestCoerceStatus:
    """Cover _coerce_status validator edge cases."""

    def test_status_ok(self):
        ps = PatchSet(status="ok")
        assert ps.status == "ok"

    def test_status_blocked(self):
        ps = PatchSet(status="blocked")
        assert ps.status == "blocked"

    def test_status_empty_string_defaults_ok(self):
        ps = PatchSet(status="")
        assert ps.status == "ok"

    def test_status_none_defaults_ok(self):
        ps = PatchSet(status=None)  # type: ignore[arg-type]
        assert ps.status == "ok"

    def test_status_success_defaults_ok(self):
        ps = PatchSet(status="success")  # type: ignore[arg-type]
        assert ps.status == "ok"

    def test_status_complete_defaults_ok(self):
        ps = PatchSet(status="complete")  # type: ignore[arg-type]
        assert ps.status == "ok"

    def test_status_random_string_defaults_ok(self):
        ps = PatchSet(status="anything_else")  # type: ignore[arg-type]
        assert ps.status == "ok"

    def test_status_zero_defaults_ok(self):
        """Falsy integer 0 should default to ok."""
        ps = PatchSet(status=0)  # type: ignore[arg-type]
        assert ps.status == "ok"

    def test_status_false_defaults_ok(self):
        """Boolean False should default to ok."""
        ps = PatchSet(status=False)  # type: ignore[arg-type]
        assert ps.status == "ok"


class TestFileValues:
    """``files`` values are normalised to whole-file strings or rejected, never repr()'d."""

    def test_plain_string_values(self):
        ps = PatchSet(files={"main.py": "print('hello')"})
        assert ps.files == {"main.py": "print('hello')"}

    def test_empty_files(self):
        assert PatchSet(files={}).files == {}
        assert PatchSet.model_validate({"files": None}).files == {}

    @pytest.mark.parametrize("key", ["content", "code", "text", "source"])
    def test_wrapped_content_is_unwrapped(self, key: str):
        ps = PatchSet.model_validate({"files": {"main.py": {key: "code here"}}})
        assert ps.files == {"main.py": "code here"}

    def test_line_list_is_joined(self):
        ps = PatchSet.model_validate({"files": {"calc.py": ["import os", "print(1)"]}})
        assert ps.files == {"calc.py": "import os\nprint(1)"}

    def test_lines_with_newlines_are_kept_verbatim(self):
        ps = PatchSet.model_validate({"files": {"calc.py": ["import os\n", "print(1)\n"]}})
        assert ps.files == {"calc.py": "import os\nprint(1)\n"}

    def test_single_whole_file_change_is_accepted(self):
        value = {"changes": [{"type": "modify", "content": "full file"}]}
        ps = PatchSet.model_validate({"files": {"main.py": value}})
        assert ps.files == {"main.py": "full file"}

    def test_list_of_path_objects_becomes_a_mapping(self):
        ps = PatchSet.model_validate(
            {
                "files": [
                    {"path": "a.py", "content": "a = 1"},
                    {"filename": "b.py", "code": "b = 2"},
                    {"file": "old.py", "action": "delete"},
                ]
            }
        )
        assert ps.files == {"a.py": "a = 1", "b.py": "b = 2"}
        assert ps.delete_files == ["old.py"]

    @pytest.mark.parametrize(
        "value",
        [
            42,
            3.14,
            True,
            [1, 2, 3],
            {"weird": "format"},
            {"content": None},
            {"content": 99},
            {"changes": []},
            {"changes": [{"content": "line1"}, {"content": "line2"}]},
            {"changes": [{"type": "modify", "line": 10, "content": "return a + b"}]},
            {"changes": [{"type": "modify"}]},
        ],
        ids=[
            "int",
            "float",
            "bool",
            "int-list",
            "unknown-dict",
            "null-content",
            "int-content",
            "no-changes",
            "multi-change",
            "line-edit",
            "change-without-content",
        ],
    )
    def test_unusable_values_are_rejected_with_the_expected_format(self, value):
        with pytest.raises(ValidationError) as exc_info:
            PatchSet.model_validate({"files": {"main.py": value}})
        message = str(exc_info.value)
        assert "complete file content as a JSON string" in message
        assert '"delete_files"' in message

    def test_rejected_value_never_written_as_repr(self):
        with pytest.raises(ValidationError):
            PatchSet.model_validate({"files": {"a.py": "ok", "b.py": {"action": "rename"}}})


class TestDeletions:
    @pytest.mark.parametrize("marker", [None, "<delete>", "__DELETE__", {"action": "delete"}])
    def test_delete_markers_move_the_path_to_delete_files(self, marker):
        ps = PatchSet.model_validate({"files": {"keep.py": "x = 1", "old.py": marker}})
        assert ps.files == {"keep.py": "x = 1"}
        assert ps.delete_files == ["old.py"]

    def test_null_is_never_written_as_text(self):
        ps = PatchSet.model_validate({"files": {"calc.py": None}})
        assert "None" not in ps.files.values()
        assert ps.files == {}

    def test_moved_file(self):
        ps = PatchSet.model_validate(
            {"files": {"tests/test_a.py": "def test_a(): ..."}, "delete_files": ["test_a.py"]}
        )
        assert ps.files == {"tests/test_a.py": "def test_a(): ..."}
        assert ps.delete_files == ["test_a.py"]

    def test_a_path_cannot_be_written_and_deleted(self):
        with pytest.raises(ValidationError, match="both"):
            PatchSet.model_validate({"files": {"a.py": "x"}, "delete_files": ["./a.py"]})

    def test_duplicate_deletions_collapse(self):
        ps = PatchSet.model_validate({"files": {"a.py": None}, "delete_files": ["a.py", "a.py"]})
        assert ps.delete_files == ["a.py"]

    def test_single_path_string_is_accepted(self):
        assert PatchSet.model_validate({"delete_files": "old.py"}).delete_files == ["old.py"]


class TestPaths:
    def test_fixable_paths_are_normalised(self):
        ps = PatchSet(files={"./b/c.py": "", "d\\e.py": "", "f//g.py": ""})
        assert set(ps.files) == {"b/c.py", "d/e.py", "f/g.py"}

    @pytest.mark.parametrize(
        "path", ["/a.py", "../x.py", "a/../../x.py", "C:/x.py", "//host/share/x.py"]
    )
    def test_unsafe_paths_are_kept_for_the_workspace_to_reject(self, path: str):
        ps = PatchSet.model_validate({"files": {path: "x"}, "delete_files": [path + ".old"]})
        assert ps.files == {path: "x"}
        assert ps.delete_files == [path + ".old"]

    def test_paths_that_collide_after_normalising_are_rejected(self):
        with pytest.raises(ValidationError, match="more than once"):
            PatchSet.model_validate({"files": {"a.py": "1", "./a.py": "2"}})

    def test_delete_paths_are_normalised(self):
        ps = PatchSet.model_validate({"delete_files": ["src\\old.py", "./x.py"]})
        assert ps.delete_files == ["src/old.py", "x.py"]

    def test_non_string_path_is_rejected(self):
        with pytest.raises(ValidationError):
            PatchSet.model_validate({"files": [{"path": 3, "content": "x"}]})


class TestPatchSetDefaults:
    """Ensure defaults are sensible."""

    def test_all_defaults(self):
        ps = PatchSet()
        assert ps.status == "ok"
        assert ps.files == {}
        assert ps.delete_files == []
        assert ps.diff_unified == ""
        assert ps.rationale == ""
        assert ps.tests_added_or_updated == []
        assert ps.tests_to_run == []
        assert ps.questions == []

    def test_model_validate_from_dict(self):
        data = {
            "status": "ok",
            "files": {"x.py": "pass"},
            "rationale": "because",
        }
        ps = PatchSet.model_validate(data)
        assert ps.rationale == "because"
        assert ps.files == {"x.py": "pass"}

    def test_model_dump_round_trip(self):
        """model_dump() output should be re-validatable."""
        ps = PatchSet(
            status="ok",
            files={"a.py": "code"},
            rationale="reason",
            tests_to_run=["test_a.py"],
        )
        dumped = ps.model_dump()
        ps2 = PatchSet.model_validate(dumped)
        assert ps2.status == ps.status
        assert ps2.files == ps.files
        assert ps2.rationale == ps.rationale
        assert ps2.tests_to_run == ps.tests_to_run

    def test_full_construction(self):
        """Test construction with all fields populated."""
        ps = PatchSet(
            status="blocked",
            files={"main.py": "code"},
            diff_unified="--- a/main.py\n+++ b/main.py",
            rationale="blocked because tests fail",
            tests_added_or_updated=["test_main.py"],
            tests_to_run=["pytest test_main.py"],
            questions=["Should I add type hints?"],
        )
        assert ps.status == "blocked"
        assert ps.diff_unified.startswith("---")
        assert len(ps.questions) == 1


def test_json_schema_file_matches_model():
    """schemas/patch_set.schema.json lists every PatchSet field."""
    path = Path(__file__).resolve().parents[2] / "schemas" / "patch_set.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert set(schema["properties"]) == set(PatchSet.model_json_schema()["properties"])


class _ScriptedProvider(BaseProvider):
    """Returns the scripted responses in order and records each user message."""

    def __init__(self, *contents: str) -> None:
        self._contents = list(contents)
        self.messages: list[str] = []

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        self.messages.append(user_message)
        return ProviderResponse(content=self._contents.pop(0), model="mock")


async def test_implementer_retries_a_bad_file_value_with_the_format():
    provider = _ScriptedProvider(
        json.dumps({"files": {"calc.py": {"changes": [{"line": 3, "content": "return a + b"}]}}}),
        json.dumps({"files": {"calc.py": "def add(a, b):\n    return a + b\n"}}),
    )
    agent = ImplementerAgent(provider=provider, system_prompt="sys")

    result = await agent.invoke({"task": {"task_id": "t1"}})

    assert result["files"] == {"calc.py": "def add(a, b):\n    return a + b\n"}
    assert "complete file content as a JSON string" in provider.messages[1]


class TestEdits:
    """Search/replace edits of existing files (#98, D12)."""

    def test_edits_validate_and_normalise_paths(self):
        ps = PatchSet.model_validate(
            {"edits": [{"path": r".\src\engine.py", "search": "a = 1", "replace": "a = 2"}]}
        )
        assert ps.files == {}
        assert [(e.path, e.search, e.replace, e.count) for e in ps.edits] == [
            ("src/engine.py", "a = 1", "a = 2", 1)
        ]

    def test_search_replace_file_value_becomes_an_edit_not_a_file(self):
        ps = PatchSet.model_validate(
            {"files": {"engine.py": {"search": "x = 1", "replace": "x = 2"}, "new.py": "y = 1"}}
        )
        assert ps.files == {"new.py": "y = 1"}
        assert [(e.path, e.search, e.replace) for e in ps.edits] == [
            ("engine.py", "x = 1", "x = 2")
        ]

    def test_edits_keyed_by_path(self):
        ps = PatchSet.model_validate(
            {
                "edits": {
                    "a.py": {"search": "1", "replace": "2"},
                    "b.py": [{"search": "x", "replace": "y"}, {"search": "z", "replace": ""}],
                }
            }
        )
        assert [(e.path, e.search) for e in ps.edits] == [
            ("a.py", "1"),
            ("b.py", "x"),
            ("b.py", "z"),
        ]

    def test_null_replace_deletes_the_text(self):
        ps = PatchSet.model_validate({"edits": [{"path": "a.py", "search": "x", "replace": None}]})
        assert ps.edits[0].replace == ""

    @pytest.mark.parametrize(
        "edit",
        [
            {"path": "a.py", "search": "", "replace": "x"},
            {"path": "a.py", "search": "x", "replace": "y", "count": 0},
            {"path": "a.py", "search": "x", "replace": "y", "count": True},
            {"search": "x", "replace": "y"},
        ],
        ids=["empty-search", "zero-count", "bool-count", "no-path"],
    )
    def test_invalid_edits_are_rejected(self, edit):
        with pytest.raises(ValidationError):
            PatchSet.model_validate({"edits": [edit]})

    def test_a_path_cannot_be_written_and_edited(self):
        with pytest.raises(ValidationError, match='both in "files" and in "edits"'):
            PatchSet.model_validate(
                {
                    "files": {"a.py": "x"},
                    "edits": [{"path": "./a.py", "search": "x", "replace": "y"}],
                }
            )

    def test_a_path_cannot_be_edited_and_deleted(self):
        with pytest.raises(ValidationError, match='both in "edits" and in "delete_files"'):
            PatchSet.model_validate(
                {
                    "edits": [{"path": "a.py", "search": "x", "replace": "y"}],
                    "delete_files": ["a.py"],
                }
            )

    def test_apply_replaces_an_exact_unique_match(self):
        edit = FileEdit(path="a.py", search="return 1\n", replace="return 2\n")
        assert edit.apply("def f():\n    return 1\n") == "def f():\n    return 2\n"

    @pytest.mark.parametrize("content", ["def f():\n    return 0\n", "return 1\nreturn 1\n"])
    def test_apply_rejects_a_missing_or_ambiguous_match(self, content):
        edit = FileEdit(path="a.py", search="return 1", replace="return 2")
        with pytest.raises(EditMismatchError, match=r"a\.py: the search text occurs"):
            edit.apply(content)

    def test_count_replaces_every_expected_occurrence(self):
        edit = FileEdit(path="a.py", search="x", replace="y", count=2)
        assert edit.apply("x + x") == "y + y"
        with pytest.raises(EditMismatchError):
            edit.apply("x")

    def test_lf_search_matches_a_crlf_file(self):
        edit = FileEdit(path="a.py", search="a = 1\nb = 2\n", replace="a = 1\nb = 3\n")
        assert edit.apply("a = 1\r\nb = 2\r\n") == "a = 1\r\nb = 3\r\n"

    def test_apply_edits_in_order_and_stops_at_the_first_mismatch(self):
        edits = [
            FileEdit(path="a.py", search="1", replace="2"),
            FileEdit(path="a.py", search="2", replace="3"),
        ]
        assert apply_edits("x = 1", edits) == "x = 3"
        with pytest.raises(EditMismatchError):
            apply_edits("x = 5", edits)

    def test_json_schema_documents_edits(self):
        schema = PatchSet.model_json_schema()
        assert "edits" in schema["properties"]
        assert set(schema["$defs"]["FileEdit"]["properties"]) == {
            "path",
            "search",
            "replace",
            "count",
        }
        path = Path(__file__).resolve().parents[2] / "schemas" / "patch_set.schema.json"
        items = json.loads(path.read_text(encoding="utf-8"))["properties"]["edits"]["items"]
        assert set(items["properties"]) == set(FileEdit.model_fields)
        assert items["required"] == ["path", "search"]


class TestEmptyLineList:
    """An empty list is as ambiguous as null: rejected, never written as an empty file (#109)."""

    @pytest.mark.parametrize("value", [[], {"content": []}])
    def test_empty_list_is_rejected(self, value):
        with pytest.raises(ValidationError, match="complete file content"):
            PatchSet.model_validate({"files": {"main.py": value}})


def test_an_applied_edit_is_an_idempotent_no_op():
    edit = FileEdit(path="a.py", search="x = 1\n", replace="x = 2\n")
    once = edit.apply("x = 1\ny = 0\n")
    assert edit.apply(once) == once == "x = 2\ny = 0\n"
    with pytest.raises(EditMismatchError):
        FileEdit(path="a.py", search="x = 1", replace="").apply("y = 0")
