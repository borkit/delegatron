"""Tests for Docker-named-volume caching of workspace-local dep dirs.

Regression for the "React generation loses node_modules between iterations"
bug: the old design moved ``node_modules`` into the image filesystem and
symlinked it back through the bind-mount; that fails on Docker Desktop
(WSL2/macOS) because symlinks inside a bind-mount don't resolve reliably
from a fresh container.

Fix: mount a Docker named volume OVER ``/workspace/node_modules`` (or
``/workspace/vendor`` for PHP).  The volume persists across container runs,
so iteration 2 sees the dep dir populated from iteration 1.
"""

from __future__ import annotations

from delegatron.sandbox.docker_runner import (
    _cache_volume_mounts,
    _cache_volume_name,
)


class TestCacheVolumeName:
    def test_deterministic_for_same_inputs(self) -> None:
        assert _cache_volume_name("abc123", "node_modules") == _cache_volume_name(
            "abc123", "node_modules"
        )

    def test_different_cache_keys_yield_different_names(self) -> None:
        a = _cache_volume_name("abc123", "node_modules")
        b = _cache_volume_name("def456", "node_modules")
        assert a != b

    def test_different_dirs_yield_different_names(self) -> None:
        a = _cache_volume_name("abc123", "node_modules")
        b = _cache_volume_name("abc123", "vendor")
        assert a != b

    def test_non_alnum_in_dir_is_sanitized(self) -> None:
        # Docker volume names must match [a-zA-Z0-9][a-zA-Z0-9_.-]+
        name = _cache_volume_name("abc", "weird/dir-name")
        assert "/" not in name
        assert "-" in name  # prefix uses hyphens by design


class TestCacheVolumeMounts:
    def test_javascript_gets_node_modules_mount(self) -> None:
        args, names = _cache_volume_mounts("javascript", "k1", "/workspace", is_windows=False)
        assert len(args) == 2
        assert args[0] == "-v"
        assert args[1].endswith(":/workspace/node_modules")
        assert names == [_cache_volume_name("k1", "node_modules")]

    def test_typescript_same_as_javascript(self) -> None:
        js_args, js_names = _cache_volume_mounts("javascript", "k1", "/workspace", is_windows=False)
        ts_args, ts_names = _cache_volume_mounts("typescript", "k1", "/workspace", is_windows=False)
        assert js_args == ts_args
        assert js_names == ts_names

    def test_php_gets_vendor_mount(self) -> None:
        args, names = _cache_volume_mounts("php", "k1", "/workspace", is_windows=False)
        assert len(args) == 2
        assert args[1].endswith(":/workspace/vendor")
        assert names[0] == _cache_volume_name("k1", "vendor")

    def test_python_no_mounts(self) -> None:
        # Python deps live outside the workspace (site-packages), so no
        # workspace-local cache is needed.
        args, names = _cache_volume_mounts("python", "k1", "/workspace", is_windows=False)
        assert args == []
        assert names == []

    def test_unknown_language_no_mounts(self) -> None:
        args, names = _cache_volume_mounts("cobol", "k1", "/workspace", is_windows=False)
        assert args == []
        assert names == []

    def test_none_language_no_mounts(self) -> None:
        args, names = _cache_volume_mounts(None, "k1", "/workspace", is_windows=False)
        assert args == []
        assert names == []

    def test_windows_mode_no_mounts(self) -> None:
        """Named volumes over bind-mounted paths are unreliable on Windows."""
        args, names = _cache_volume_mounts("javascript", "k1", "C:\\workspace", is_windows=True)
        assert args == []
        assert names == []

    def test_different_cache_keys_produce_independent_volumes(self) -> None:
        _, a = _cache_volume_mounts("javascript", "k1", "/workspace", is_windows=False)
        _, b = _cache_volume_mounts("javascript", "k2", "/workspace", is_windows=False)
        # Different install_cmds (hence different cache_key) should not share
        # the volume — otherwise changing deps would silently reuse stale ones.
        assert a != b

    def test_workspace_mount_path_flows_through(self) -> None:
        args, _ = _cache_volume_mounts("javascript", "k1", "/custom/work", is_windows=False)
        assert args[1].endswith(":/custom/work/node_modules")
