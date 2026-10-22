"""PatchSet schema — GPT implementer output."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Literal, cast

from pydantic import BaseModel, field_validator, model_validator

from delegatron.utils.paths import normalize_rel_path

logger = logging.getLogger(__name__)

# "files" values (besides null) that mean "delete this file".
_DELETE_MARKERS = frozenset({"<delete>", "__DELETE__"})
# Keys under which models wrap a whole file: {"content": "..."}.
_CONTENT_KEYS = ("content", "code", "text", "source")
# Keys naming the path in a list-shaped "files": [{"path": ..., "content": ...}].
_PATH_KEYS = ("path", "file", "filename")
# Keys that address part of a file; such an edit cannot be applied as the whole file.
_PARTIAL_EDIT_KEYS = frozenset({"line", "lines", "start", "end", "range", "old", "search"})

_FORMAT_HINT = (
    '"files" values must be the complete file content as a JSON string; '
    'to change part of a file, use "edits"; '
    'to delete a file, list its path in "delete_files"'
)


class EditMismatchError(ValueError):
    """A search/replace edit's ``search`` text does not occur exactly ``count`` times."""


class FileEdit(BaseModel):
    """A search/replace edit of one existing file, applied by the orchestrator.

    ``search`` must occur exactly ``count`` times in the file's current content;
    every occurrence is replaced by ``replace``.  The path is normalised like a
    ``files`` key.
    """

    path: str
    search: str
    replace: str = ""
    count: int = 1

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, v: Any) -> str:
        return _clean_path(v)

    @field_validator("search")
    @classmethod
    def _search_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError('the "search" text of an edit must not be empty')
        return v

    @field_validator("replace", mode="before")
    @classmethod
    def _replace_none_is_empty(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("count", mode="before")
    @classmethod
    def _count_at_least_one(cls, v: Any) -> Any:
        if v is None:
            return 1
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError('the "count" of an edit must be a positive integer')
        return v

    def apply(self, content: str) -> str:
        """Return *content* with this edit applied; raise :class:`EditMismatchError`.

        A file with CRLF line endings also matches an LF ``search`` text (the
        replacement then gets CRLF endings too).  An edit already applied (no
        ``search`` match, ``replace`` found ``count`` times) changes nothing, so
        a retried patch is idempotent.
        """
        search, replace = self.search, self.replace
        found = content.count(search)
        if not found and "\r\n" in content and "\r\n" not in search:
            search, replace = search.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
            found = content.count(search)
        if not found and replace and content.count(replace) == self.count:
            return content
        if found != self.count:
            expected = "once" if self.count == 1 else f"{self.count} times"
            raise EditMismatchError(
                f"{self.path}: the search text occurs {found} time(s), expected {expected}"
            )
        return content.replace(search, replace)


def apply_edits(content: str, edits: Iterable[FileEdit]) -> str:
    """Apply *edits* to *content* in order; the first mismatch raises :class:`EditMismatchError`."""
    for edit in edits:
        content = edit.apply(content)
    return content


class PatchSet(BaseModel):
    """Structured output from the implementer agent.

    ``files`` maps repo-relative paths to complete new contents, ``edits``
    changes part of existing files (search/replace, see :class:`FileEdit`) and
    ``delete_files`` lists paths to remove.  Paths are normalised with
    :func:`~delegatron.utils.paths.normalize_rel_path`; a path it rejects
    (absolute, drive, UNC, ``..``) is kept as given so the workspace and the
    sandboxes skip and report it.
    """

    status: Literal["ok", "blocked"] = "ok"
    files: dict[str, str] = {}
    edits: list[FileEdit] = []
    delete_files: list[str] = []
    diff_unified: str = ""
    rationale: str = ""
    tests_added_or_updated: list[str] = []
    tests_to_run: list[str] = []
    questions: list[str] = []

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, v: Any) -> str:
        """Normalise status values from local models.

        Local models sometimes return empty string, None, or non-standard
        values like "success" or "complete".  Map these to "ok"/"blocked".
        """
        if not v or v not in ("ok", "blocked"):
            return "ok"
        return cast(str, v)

    @model_validator(mode="before")
    @classmethod
    def _normalize_files(cls, data: Any) -> Any:
        """Normalise ``files`` to ``{path: content}`` and collect edits and deletions.

        Accepted shapes: a content string; a list of lines; ``{"content"|"code"|
        "text"|"source": str}``; ``{"changes": [one whole-file entry]}``; and a
        list of ``{"path", "content"}`` objects instead of a mapping.  ``null``,
        ``"<delete>"``, ``"__DELETE__"`` and ``{"action": "delete"}`` move the
        path to ``delete_files``; a ``{"search", "replace"}`` value moves to
        ``edits``.  Any other value (an empty list too) is rejected, never
        written as its Python repr, so the model is asked again with the
        format.  ``edits`` may also map each path to one edit or a list of them.
        A path may be in only one of ``files``, ``edits`` and ``delete_files``.
        """
        if not isinstance(data, dict):
            return data
        raw_files = data.get("files")
        if raw_files is not None and not isinstance(raw_files, dict | list):
            return data  # left for the field's own type error
        files: dict[str, str] = {}
        deletes: list[str] = []
        edits = _edit_entries(data.get("edits"))
        for raw_path, value in _file_entries(raw_files):
            path = _clean_path(raw_path)
            if path in files or path in deletes:
                raise ValueError(f'"files" lists {path!r} more than once')
            if _is_edit(value):
                edits.append({**value, "path": path})
                continue
            content = _file_content(path, value)
            if content is None:
                deletes.append(path)
            else:
                files[path] = content
        for raw_path in _path_list(data.get("delete_files")):
            path = _clean_path(raw_path)
            if path in files:
                raise ValueError(f'{path!r} is both in "files" and in "delete_files"')
            if path not in deletes:
                deletes.append(path)
        for edit in edits:
            raw_path = edit.get("path") if isinstance(edit, dict) else None
            edit_path = _clean_path(raw_path) if isinstance(raw_path, str) else None
            if edit_path in files:
                raise ValueError(f'{edit_path!r} is both in "files" and in "edits"')
            if edit_path in deletes:
                raise ValueError(f'{edit_path!r} is both in "edits" and in "delete_files"')
        return {**data, "files": files, "edits": edits, "delete_files": deletes}


def _is_edit(value: Any) -> bool:
    """Whether a ``files`` value is a search/replace edit rather than a whole file."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("search"), str)
        and isinstance(value.get("replace"), str)
    )


def _edit_entries(edits: Any) -> list[Any]:
    """``edits`` as a list: ``None`` -> ``[]``; ``{path: edit | [edits]}`` -> edits with paths."""
    if edits is None:
        return []
    if isinstance(edits, list):
        return list(edits)
    if isinstance(edits, dict) and "search" not in edits:
        entries: list[Any] = []
        for path, value in edits.items():
            for edit in value if isinstance(value, list) else [value]:
                entries.append({**edit, "path": path} if isinstance(edit, dict) else edit)
        return entries
    return [edits]


def _file_entries(files: Any) -> list[tuple[Any, Any]]:
    """``(path, value)`` pairs from a ``{path: value}`` mapping or a list of objects."""
    if files is None:
        return []
    if isinstance(files, dict):
        return list(files.items())
    entries: list[tuple[Any, Any]] = []
    for item in files:
        path_key = next((k for k in _PATH_KEYS if isinstance(item, dict) and k in item), None)
        if path_key is None:
            raise ValueError(f'"files" must map each path to its content; {_FORMAT_HINT}')
        entries.append((item[path_key], {k: v for k, v in item.items() if k != path_key}))
    return entries


def _clean_path(path: Any) -> str:
    """The normalised path, or *path* unchanged when it is unsafe (reported downstream)."""
    if not isinstance(path, str):
        raise ValueError(f"file paths must be strings, got {path!r}")
    return normalize_rel_path(path) or path


def _path_list(value: Any) -> list[Any]:
    """``delete_files`` as a list: ``None`` → ``[]``, a single path → ``[path]``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    raise ValueError('"delete_files" must be a list of paths')


def _file_content(path: str, value: Any) -> str | None:
    """The complete file content for *value*, or ``None`` when it asks for deletion."""
    if value is None:
        logger.warning("PatchSet: %r has a null value; deleting the file", path)
        return None
    if isinstance(value, str):
        return None if value.strip() in _DELETE_MARKERS else value
    lines = _joined_lines(value)
    if lines is not None:
        return lines
    if isinstance(value, dict):
        if str(value.get("action") or value.get("type") or "").lower() == "delete":
            return None
        for key in _CONTENT_KEYS:
            if key in value:
                content = value[key]
                if isinstance(content, str):
                    return content
                lines = _joined_lines(content)
                if lines is not None:
                    return lines
                break
        changes = value.get("changes")
        if (
            isinstance(changes, list)
            and len(changes) == 1
            and isinstance(changes[0], dict)
            and not _PARTIAL_EDIT_KEYS & changes[0].keys()
            and isinstance(changes[0].get("content"), str)
        ):
            return cast(str, changes[0]["content"])
    raise ValueError(f"invalid value for {path!r}: {_FORMAT_HINT}")


def _joined_lines(value: Any) -> str | None:
    """A list of line strings as file content (kept as-is if they end in newlines).

    An empty list is as ambiguous as ``null``: it is not content (``None``).
    """
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(line, str) for line in value):
        return None
    if all(line.endswith("\n") for line in value):
        return "".join(value)
    return "\n".join(value)
