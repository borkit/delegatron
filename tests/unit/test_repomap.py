"""Tests for repo map — structural overview generation."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from delegatron import repomap
from delegatron.repomap import (
    FileSymbols,
    Symbol,
    _collect_files,
    _extract_basic_symbols,
    _extract_python_symbols,
    _format_func_signature,
    _format_map,
    _rank_files,
    generate_repo_map,
    list_workspace_files,
)

# ---------------------------------------------------------------------------
# Symbol / FileSymbols dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Construction and defaults for Symbol and FileSymbols."""

    def test_symbol_minimal(self):
        s = Symbol(name="foo", kind="function", file="a.py", line=1)
        assert s.name == "foo"
        assert s.kind == "function"
        assert s.file == "a.py"
        assert s.line == 1
        assert s.signature == ""
        assert s.parent == ""

    def test_symbol_full(self):
        s = Symbol(
            name="bar",
            kind="method",
            file="b.py",
            line=42,
            signature="def bar(self, x: int) -> str",
            parent="MyClass",
        )
        assert s.parent == "MyClass"
        assert s.signature == "def bar(self, x: int) -> str"

    def test_file_symbols_defaults(self):
        fs = FileSymbols(path="src/mod.py")
        assert fs.path == "src/mod.py"
        assert fs.symbols == []
        assert fs.imports == []

    def test_file_symbols_with_data(self):
        sym = Symbol(name="A", kind="class", file="x.py", line=1)
        fs = FileSymbols(path="x.py", symbols=[sym], imports=["os", "sys"])
        assert len(fs.symbols) == 1
        assert fs.imports == ["os", "sys"]


# ---------------------------------------------------------------------------
# _extract_python_symbols
# ---------------------------------------------------------------------------


class TestExtractPythonSymbols:
    """Python AST-based extraction."""

    def test_class_and_methods(self, tmp_path: Path):
        src = tmp_path / "mod.py"
        src.write_text(
            "class Greeter:\n    def greet(self, name: str) -> str:\n        return f'hi {name}'\n"
        )
        result = _extract_python_symbols(src, "mod.py")
        assert result is not None
        names = [s.name for s in result.symbols]
        assert "Greeter" in names
        assert "greet" in names
        method = next(s for s in result.symbols if s.name == "greet")
        assert method.kind == "method"
        assert method.parent == "Greeter"
        assert "str" in method.signature

    def test_top_level_function(self, tmp_path: Path):
        src = tmp_path / "funcs.py"
        src.write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
        result = _extract_python_symbols(src, "funcs.py")
        assert result is not None
        assert len(result.symbols) == 1
        sym = result.symbols[0]
        assert sym.kind == "function"
        assert "int" in sym.signature

    def test_async_function(self, tmp_path: Path):
        src = tmp_path / "async_mod.py"
        src.write_text("async def fetch(url: str) -> bytes:\n    return b''\n")
        result = _extract_python_symbols(src, "async_mod.py")
        assert result is not None
        sym = result.symbols[0]
        assert sym.signature.startswith("async def")
        assert "url: str" in sym.signature
        assert "-> bytes" in sym.signature

    def test_imports_extracted(self, tmp_path: Path):
        src = tmp_path / "imp.py"
        src.write_text("import os\nimport sys\nfrom pathlib import Path\n\ndef f(): pass\n")
        result = _extract_python_symbols(src, "imp.py")
        assert result is not None
        assert "os" in result.imports
        assert "sys" in result.imports
        assert "pathlib" in result.imports

    def test_class_with_bases(self, tmp_path: Path):
        src = tmp_path / "bases.py"
        src.write_text("class Dog(Animal):\n    pass\n")
        result = _extract_python_symbols(src, "bases.py")
        assert result is not None
        cls = result.symbols[0]
        assert cls.kind == "class"
        assert "Animal" in cls.signature

    def test_syntax_error_returns_none(self, tmp_path: Path):
        src = tmp_path / "bad.py"
        src.write_text("def broken(\n")
        result = _extract_python_symbols(src, "bad.py")
        assert result is None

    def test_empty_file(self, tmp_path: Path):
        src = tmp_path / "empty.py"
        src.write_text("")
        result = _extract_python_symbols(src, "empty.py")
        assert result is not None
        assert result.symbols == []


# ---------------------------------------------------------------------------
# _format_func_signature
# ---------------------------------------------------------------------------


class TestFormatFuncSignature:
    """Signature formatting from AST nodes."""

    def _parse_func(self, code: str):
        import ast

        tree = ast.parse(code)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        raise ValueError("No function found")

    def test_typed_args_and_return(self):
        node = self._parse_func("def add(a: int, b: int) -> int: pass")
        sig = _format_func_signature(node)
        assert sig == "def add(a: int, b: int) -> int"

    def test_no_annotations(self):
        node = self._parse_func("def plain(x, y): pass")
        sig = _format_func_signature(node)
        assert sig == "def plain(x, y)"

    def test_async_function(self):
        node = self._parse_func("async def run(cmd: str) -> None: pass")
        sig = _format_func_signature(node)
        assert sig.startswith("async def")
        assert "cmd: str" in sig
        assert "-> None" in sig

    def test_no_args(self):
        node = self._parse_func("def noop(): pass")
        sig = _format_func_signature(node)
        assert sig == "def noop()"

    def test_self_arg(self):
        import ast

        code = "class C:\n  def method(self, x: int): pass"
        tree = ast.parse(code)
        cls = next(n for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef))
        method = next(n for n in ast.iter_child_nodes(cls) if isinstance(n, ast.FunctionDef))
        sig = _format_func_signature(method)
        assert "self" in sig
        assert "x: int" in sig


# ---------------------------------------------------------------------------
# _extract_basic_symbols (regex fallback)
# ---------------------------------------------------------------------------


class TestExtractBasicSymbols:
    """Regex-based symbol extraction for non-Python files."""

    def test_javascript_function(self, tmp_path: Path):
        src = tmp_path / "app.js"
        src.write_text("function handleClick(event) {\n  console.log(event)\n}\n")
        result = _extract_basic_symbols(src, "app.js")
        assert result is not None
        assert any(s.name == "handleClick" for s in result.symbols)

    def test_exported_class(self, tmp_path: Path):
        src = tmp_path / "comp.ts"
        src.write_text("export class UserService {\n  constructor() {}\n}\n")
        result = _extract_basic_symbols(src, "comp.ts")
        assert result is not None
        assert any(s.name == "UserService" for s in result.symbols)

    def test_go_func(self, tmp_path: Path):
        src = tmp_path / "main.go"
        src.write_text("func ServeHTTP(w http.ResponseWriter, r *http.Request) {\n}\n")
        result = _extract_basic_symbols(src, "main.go")
        assert result is not None
        assert any(s.name == "ServeHTTP" for s in result.symbols)

    def test_rust_fn(self, tmp_path: Path):
        src = tmp_path / "lib.rs"
        src.write_text("pub fn process(input: &str) -> Result<()> {\n}\n")
        result = _extract_basic_symbols(src, "lib.rs")
        assert result is not None
        assert any(s.name == "process" for s in result.symbols)

    def test_skips_private_helpers(self, tmp_path: Path):
        src = tmp_path / "helper.py"
        src.write_text("def _private():\n  pass\ndef public():\n  pass\n")
        result = _extract_basic_symbols(src, "helper.py")
        assert result is not None
        names = [s.name for s in result.symbols]
        assert "public" in names
        assert "_private" not in names

    def test_empty_file_returns_none(self, tmp_path: Path):
        src = tmp_path / "empty.js"
        src.write_text("// just a comment\n")
        result = _extract_basic_symbols(src, "empty.js")
        assert result is None

    def test_async_function_js(self, tmp_path: Path):
        src = tmp_path / "api.js"
        src.write_text("async function fetchData(url) {\n  return await fetch(url)\n}\n")
        result = _extract_basic_symbols(src, "api.js")
        assert result is not None
        assert any(s.name == "fetchData" for s in result.symbols)


# ---------------------------------------------------------------------------
# _rank_files
# ---------------------------------------------------------------------------


class TestRankFiles:
    """File relevance ranking."""

    def _make_fs(self, path: str, symbol_names: list[str], kinds: str = "function") -> FileSymbols:
        return FileSymbols(
            path=path,
            symbols=[
                Symbol(name=n, kind=kinds, file=path, line=i + 1)
                for i, n in enumerate(symbol_names)
            ],
        )

    def test_mentioned_files_ranked_highest(self):
        fs_a = self._make_fs("src/auth.py", ["login", "logout"])
        fs_b = self._make_fs("src/utils.py", ["helper"])
        ranked = _rank_files([fs_a, fs_b], "", ["src/auth.py"])
        assert ranked[0].path == "src/auth.py"

    def test_symbol_name_match_boosts(self):
        fs_a = self._make_fs("src/user.py", ["create_user", "delete_user"])
        fs_b = self._make_fs("src/config.py", ["load_config"])
        ranked = _rank_files([fs_a, fs_b], "create_user endpoint", [])
        assert ranked[0].path == "src/user.py"

    def test_test_files_penalized(self):
        fs_src = self._make_fs("src/core.py", ["run"])
        fs_test = self._make_fs("tests/test_core.py", ["test_run"])
        ranked = _rank_files([fs_src, fs_test], "", [])
        assert ranked[0].path == "src/core.py"

    def test_more_symbols_rank_higher(self):
        fs_big = self._make_fs("src/big.py", ["a", "b", "c", "d", "e"])
        fs_small = self._make_fs("src/small.py", ["x"])
        ranked = _rank_files([fs_big, fs_small], "", [])
        assert ranked[0].path == "src/big.py"

    def test_path_segment_in_task(self):
        fs_a = self._make_fs("src/billing/charge.py", ["charge"])
        fs_b = self._make_fs("src/utils/helper.py", ["helper"])
        ranked = _rank_files([fs_a, fs_b], "fix billing logic", [])
        assert ranked[0].path == "src/billing/charge.py"


# ---------------------------------------------------------------------------
# _format_map
# ---------------------------------------------------------------------------


class TestFormatMap:
    """Map formatting and truncation."""

    def _make_fs(self, path: str, symbols: list[Symbol]) -> FileSymbols:
        return FileSymbols(path=path, symbols=symbols)

    def test_basic_format(self):
        fs = self._make_fs(
            "src/app.py",
            [
                Symbol(
                    name="App",
                    kind="class",
                    file="src/app.py",
                    line=1,
                    signature="class App",
                ),
                Symbol(
                    name="run",
                    kind="method",
                    file="src/app.py",
                    line=5,
                    signature="def run(self)",
                    parent="App",
                ),
            ],
        )
        result = _format_map([fs], max_chars=10000)
        assert "src/app.py:" in result
        assert "class App" in result
        assert "def run(self)" in result

    def test_truncation_at_budget(self):
        files = []
        for i in range(50):
            files.append(
                self._make_fs(
                    f"src/mod_{i}.py",
                    [
                        Symbol(
                            name=f"func_{i}",
                            kind="function",
                            file=f"src/mod_{i}.py",
                            line=1,
                            signature=f"def func_{i}(x: int) -> int",
                        )
                    ],
                )
            )
        result = _format_map(files, max_chars=200)
        assert len(result) <= 300  # some slack for final partial entry
        # Should not contain all 50 files
        assert "mod_49" not in result

    def test_empty_list(self):
        result = _format_map([], max_chars=1000)
        assert result == ""

    def test_function_indentation(self):
        fs = self._make_fs(
            "lib.py",
            [
                Symbol(
                    name="standalone",
                    kind="function",
                    file="lib.py",
                    line=1,
                    signature="def standalone()",
                ),
            ],
        )
        result = _format_map([fs], max_chars=5000)
        # Standalone functions are indented with 2 spaces
        assert "  def standalone()" in result


# ---------------------------------------------------------------------------
# _collect_files
# ---------------------------------------------------------------------------


class TestCollectFiles:
    """File collection with directory/size filtering."""

    def test_collects_python_files(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("x = 1")
        (tmp_path / "readme.md").write_text("# hi")
        files = _collect_files(tmp_path)
        assert any(f.endswith("main.py") for f in files)
        assert not any(f.endswith("readme.md") for f in files)

    def test_skips_pycache(self, tmp_path: Path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-311.pyc").write_text("")
        (tmp_path / "mod.py").write_text("pass")
        files = _collect_files(tmp_path)
        assert not any("__pycache__" in f for f in files)

    def test_skips_git_dir(self, tmp_path: Path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "config.py").write_text("")
        (tmp_path / "app.py").write_text("pass")
        files = _collect_files(tmp_path)
        assert not any(".git" in f for f in files)

    def test_skips_node_modules(self, tmp_path: Path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "dep.js").write_text("")
        (tmp_path / "index.js").write_text("")
        files = _collect_files(tmp_path)
        assert not any("node_modules" in f for f in files)

    def test_skips_large_files(self, tmp_path: Path):
        big = tmp_path / "huge.py"
        big.write_text("x" * 200_000)
        small = tmp_path / "tiny.py"
        small.write_text("x = 1")
        files = _collect_files(tmp_path)
        assert any("tiny.py" in f for f in files)
        assert not any("huge.py" in f for f in files)

    def test_collects_js_ts_files(self, tmp_path: Path):
        (tmp_path / "app.js").write_text("const x = 1;")
        (tmp_path / "types.ts").write_text("type X = string;")
        files = _collect_files(tmp_path)
        assert any(f.endswith("app.js") for f in files)
        assert any(f.endswith("types.ts") for f in files)

    def test_skips_hidden_dirs(self, tmp_path: Path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("pass")
        files = _collect_files(tmp_path)
        assert not any(".hidden" in f for f in files)

    def test_returns_posix_paths(self, tmp_path: Path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        assert _collect_files(tmp_path) == ["tests/test_x.py"]

    def test_skips_run_and_sandbox_dirs(self, tmp_path: Path):
        for copy_dir in ("_sandbox/workspace", "runs/6f29441bbe8c", ".delegatron-runs/abc"):
            (tmp_path / copy_dir).mkdir(parents=True)
            (tmp_path / copy_dir / "card_deck.py").write_text("class Deck: pass\n")
        (tmp_path / "runs" / "6f29441bbe8c" / "summary.json").write_text("{}\n")
        (tmp_path / "card_deck.py").write_text("class Deck: pass\n")
        assert _collect_files(tmp_path) == ["card_deck.py"]

    def test_stat_failure_is_skipped_with_a_debug_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        (tmp_path / "card_deck.py").write_text("class Deck: pass\n")
        (tmp_path / "locked.py").write_text("class Locked: pass\n")
        stat = Path.stat

        def deny_locked(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            if self.name == "locked.py":
                raise PermissionError(13, "Access is denied", str(self))
            return stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", deny_locked)
        # Another test may have stopped "delegatron" records reaching caplog's root handler.
        monkeypatch.setattr(logging.getLogger("delegatron"), "propagate", True)

        with caplog.at_level(logging.DEBUG, logger="delegatron.repomap"):
            assert _collect_files(tmp_path) == ["card_deck.py"]

        assert "Repo map: skipping locked.py: " in caplog.text


class TestListWorkspaceFiles:
    """Disk walk shared with context gathering."""

    def test_lists_all_files_as_sorted_posix_paths(self, tmp_path: Path):
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "mod.py").write_text("")
        (tmp_path / "README.md").write_text("")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.js").write_text("")
        assert list_workspace_files(tmp_path) == ["README.md", "src/pkg/mod.py"]

    def test_empty_workspace(self, tmp_path: Path):
        assert list_workspace_files(tmp_path) == []


# ---------------------------------------------------------------------------
# _extract_tree_sitter_symbols — import failure
# ---------------------------------------------------------------------------


class TestExtractTreeSitterSymbols:
    """Tree-sitter import failure handled gracefully."""

    def test_import_failure_returns_none(self, tmp_path: Path):
        src = tmp_path / "mod.js"
        src.write_text("function hello() {}\n")
        with patch.dict("sys.modules", {"tree_sitter_language_pack": None}):
            from delegatron.repomap import _extract_tree_sitter_symbols

            result = _extract_tree_sitter_symbols(src, "mod.js")
        assert result is None


# ---------------------------------------------------------------------------
# generate_repo_map (integration)
# ---------------------------------------------------------------------------


class TestGenerateRepoMap:
    """End-to-end repo map generation with a temp workspace."""

    def test_basic_generation(self, tmp_path: Path):
        pkg = tmp_path / "src"
        pkg.mkdir()
        (pkg / "app.py").write_text(
            "class App:\n"
            "    def run(self) -> None:\n"
            "        pass\n"
            "\n"
            "def main() -> None:\n"
            "    App().run()\n"
        )
        (pkg / "utils.py").write_text("def helper(x: int) -> str:\n    return str(x)\n")
        result = generate_repo_map(tmp_path, max_tokens=2048)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "App" in result
        assert "helper" in result

    def test_empty_workspace_returns_empty(self, tmp_path: Path):
        result = generate_repo_map(tmp_path)
        assert result == ""

    def test_empty_languages_infers_from_extensions(self, tmp_path: Path):
        """Scratch projects have no marker files, so detection reports no languages."""
        (tmp_path / "card_deck.py").write_text("class Deck:\n    def shuffle(self) -> None: ...\n")
        result = generate_repo_map(tmp_path, languages=[])
        assert "class Deck" in result
        assert "def shuffle(self) -> None" in result

    def test_mentioned_nested_file_ranked_first(self, tmp_path: Path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "big.py").write_text("".join(f"def f{i}() -> None: pass\n" for i in range(30)))
        (pkg / "target.py").write_text("def wanted() -> None: pass\n")
        result = generate_repo_map(tmp_path, mentioned_files=["pkg/target.py"], max_tokens=4096)
        assert result.index("pkg/target.py:") < result.index("pkg/big.py:")

    def test_mentioned_files_appear(self, tmp_path: Path):
        (tmp_path / "important.py").write_text("def critical() -> None: pass\n")
        (tmp_path / "other.py").write_text("def boring() -> None: pass\n")
        result = generate_repo_map(
            tmp_path,
            mentioned_files=["important.py"],
            max_tokens=2048,
        )
        assert "important.py" in result

    def test_task_context_boosts_relevant(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text("def login(user: str) -> bool: pass\n")
        (tmp_path / "billing.py").write_text("def charge(amount: float) -> None: pass\n")
        result = generate_repo_map(tmp_path, task_context="fix the login flow", max_tokens=2048)
        # auth.py should appear before billing.py
        assert result.index("auth.py") < result.index("billing.py")

    def test_respects_token_budget(self, tmp_path: Path):
        # Create many files to exceed a tiny budget
        for i in range(20):
            (tmp_path / f"mod_{i}.py").write_text(
                f"class Cls{i}:\n    def method_{i}(self) -> None: pass\n"
            )
        result = generate_repo_map(tmp_path, max_tokens=64)
        # 64 tokens * 4 chars = 256 chars budget; output should be bounded
        assert len(result) < 500


# ---------------------------------------------------------------------------
# Map budget, ranking and robustness (#124), internal dirs (#121)
# ---------------------------------------------------------------------------


def _functions(path: str, count: int, width: int = 0) -> FileSymbols:
    """A file of *count* functions; *width* pads each signature to that many chars."""
    return FileSymbols(
        path=path,
        symbols=[
            Symbol(
                name=f"fn_{i}",
                kind="function",
                file=path,
                line=i + 1,
                signature=f"def fn_{i}()".ljust(width, "_"),
            )
            for i in range(count)
        ],
    )


class TestFormatMapBudget:
    """One oversized file no longer collapses the map to a single line."""

    def test_large_first_file_is_capped_and_small_files_still_render(self):
        big = _functions("src/big.py", 60, width=80)  # ~5000 chars uncapped
        small = [_functions(f"src/small_{i}.py", 1) for i in range(3)]

        result = _format_map([big, *small], max_chars=4096)

        assert "  ... +35 more" in result
        assert "def fn_24" in result
        assert "def fn_25" not in result
        for i in range(3):
            assert f"src/small_{i}.py:" in result

    def test_block_that_does_not_fit_becomes_a_stub_and_later_files_are_tried(self):
        big = _functions("src/big.py", 25, width=100)
        small = _functions("src/small.py", 1)

        result = _format_map([big, small], max_chars=400)

        assert result.startswith("src/big.py: (25 symbols)\n")
        assert "src/small.py:\n  def fn_0()" in result

    def test_never_exceeds_the_budget(self):
        ranked = [
            _functions(f"src/mod_{i}.py", count, width)
            for i, (count, width) in enumerate([(60, 90), (3, 0), (30, 40), (1, 0), (12, 120)])
        ]

        for max_chars in (0, 39, 40, 41, 100, 250, 777, 1500, 4096):
            assert len(_format_map(ranked, max_chars)) <= max_chars

    def test_mentioned_file_renders_first_and_whole_up_to_the_cap(self):
        noisy = _functions("src/noisy.py", 200)
        target = _functions("src/target.py", 10)

        ranked = _rank_files([noisy, target], "", ["src/target.py"])
        result = _format_map(ranked, max_chars=4096)

        assert result.startswith("src/target.py:\n")
        assert all(f"def fn_{i}()" in result.split("\n\n")[0] for i in range(10))


class TestRankFilesLogScale:
    def test_symbol_count_cannot_outweigh_a_task_match(self):
        huge = _functions("src/huge.py", 200)
        billing = _functions("src/billing/charge.py", 1)

        ranked = _rank_files([huge, billing], "fix billing logic", [])

        assert ranked[0].path == "src/billing/charge.py"


class TestRobustness:
    """One bad file must not empty the whole map."""

    def test_dangling_symlink_is_skipped(self, tmp_path: Path):
        (tmp_path / "card_deck.py").write_text("class CardDeck:\n    pass\n")
        try:
            os.symlink(tmp_path / "missing_target.py", tmp_path / "broken_link.py")
        except OSError:
            pytest.skip("symlinks are not available")

        assert "class CardDeck" in generate_repo_map(tmp_path)

    def test_unreadable_file_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / "card_deck.py").write_text("class CardDeck:\n    pass\n")
        (tmp_path / "locked.py").write_text("class Locked:\n    pass\n")
        read_text = Path.read_text

        def deny_locked(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == "locked.py":
                raise PermissionError(self)
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", deny_locked)

        result = generate_repo_map(tmp_path)

        assert "class CardDeck" in result
        assert "Locked" not in result

    def test_file_that_crashes_symbol_extraction_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / "card_deck.py").write_text("class CardDeck:\n    pass\n")
        (tmp_path / "deep.py").write_text("x = 1\n")
        extract = repomap._extract_python_symbols

        def crash_on_deep(full_path: Path, rel_path: str) -> FileSymbols | None:
            if rel_path == "deep.py":
                raise RecursionError("maximum recursion depth exceeded")
            return extract(full_path, rel_path)

        monkeypatch.setattr(repomap, "_extract_python_symbols", crash_on_deep)

        assert "class CardDeck" in generate_repo_map(tmp_path)

    def test_non_ascii_source_is_read_as_utf8(self, tmp_path: Path):
        (tmp_path / "cards.py").write_bytes("def pokémon_name() -> str:\n    ...\n".encode())

        assert "def pokémon_name() -> str" in generate_repo_map(tmp_path)


class TestInternalDirsAtRootOnly:
    def test_internal_dirs_are_skipped_only_at_the_root(self, tmp_path: Path):
        for rel in (
            "runs/abc/state.py",
            "runs/abc/events.jsonl",
            "reviews/review-r1/notes.py",
            "_sandbox/workspace/app.py",
            "src/runs/a.py",
            "src/reviews/b.py",
            "src/_sandbox/c.py",
        ):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text("x = 1\n")

        assert _collect_files(tmp_path) == [
            "src/_sandbox/c.py",
            "src/reviews/b.py",
            "src/runs/a.py",
        ]

    def test_user_packages_named_runs_or_reviews_are_listed(self, tmp_path: Path):
        """D4: ``runs/`` and ``reviews/`` without Delegatron's layout are user code."""
        for rel in ("runs/__init__.py", "runs/job.py", "reviews/r1/notes.py", "_sandbox/x.py"):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text("x = 1\n")

        assert list_workspace_files(tmp_path) == [
            "reviews/r1/notes.py",
            "runs/__init__.py",
            "runs/job.py",
        ]
