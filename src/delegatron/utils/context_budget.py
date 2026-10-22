"""Context-window budgeting — keep a rendered prompt inside a model's window.

Local models served by Ollama run with a fixed ``num_ctx`` and silently drop
whatever does not fit, so agents trim the least important context before the
call instead: the repository map first, then files that are only related,
least relevant first, and only then shorten the files the attempt must edit
to head+tail excerpts (never dropping them).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

# Rough characters-per-token ratio for code and English prose.
CHARS_PER_TOKEN = 3.5

DEFAULT_TRIM_KEYS: tuple[str, ...] = ("repo_map", "related_files", "existing_files")

# End of a text input (the repository map) cut to fit.
_TRUNCATION_MARKER = "\n... [truncated to fit the context window]"

# The cut in a file excerpt.  Same text as the workspace context's excerpt
# marker, so the orchestrator treats both kinds of excerpt alike.
FILE_EXCERPT_MARKER = "\n... [truncated {} bytes] ...\n"

# Characters an excerpted file keeps at least (half head, half tail).
MIN_EXCERPT_CHARS = 400


def estimate_tokens(text: str) -> int:
    """Estimate the token count of *text* (``ceil(len / 3.5)``)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass
class FitResult:
    """What :func:`fit_input_to_budget` did to an input.

    ``data`` is the fitted shallow copy; ``trimmed`` the input keys changed,
    in order; ``dropped`` / ``excerpted`` the file paths left out or shortened
    per input key; ``still_over`` whether the input still exceeds the budget.
    """

    data: dict[str, Any]
    trimmed: list[str] = field(default_factory=list)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    excerpted: dict[str, list[str]] = field(default_factory=dict)
    tokens_before: int = 0
    tokens_after: int = 0
    still_over: bool = False

    def report(self, window: int, budget: int) -> dict[str, Any]:
        """The ``_meta["context_trimmed"]`` record of a call that trimmed its input."""
        return {
            "keys": list(self.trimmed),
            "window": window,
            "budget": budget,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "dropped": {key: list(paths) for key, paths in self.dropped.items()},
            "excerpted": {key: list(paths) for key, paths in self.excerpted.items()},
            "still_over": self.still_over,
        }


def fit_input_to_budget(
    input_data: dict[str, Any],
    render: Callable[[dict[str, Any]], str],
    budget_tokens: int,
    trim_keys: Sequence[str] = DEFAULT_TRIM_KEYS,
    protected: Collection[str] = (),
) -> FitResult:
    """Shrink *input_data* until ``render(data)`` fits in *budget_tokens*.

    Keys in *trim_keys* are shrunk in order, moving on only while the
    rendered text is still over budget: strings are cut from the end, other
    non-dict values dropped, and ``{path: content}`` dicts lose their entries
    from the last one (least relevant) up — except *protected* paths (the
    files the attempt must edit).  When that is not enough, the protected
    files are shortened, largest first, to head+tail excerpts carrying
    :data:`FILE_EXCERPT_MARKER`.  Keys outside *trim_keys* are never touched,
    so the result can still exceed the budget (``still_over``).

    Idempotent: fitting a fitted input changes nothing.
    """
    data = dict(input_data)
    keep = {_norm(path) for path in protected}
    result = FitResult(data=data)

    def excess_tokens() -> int:
        return estimate_tokens(render(data)) - budget_tokens

    result.tokens_before = estimate_tokens(render(data))
    if result.tokens_before > budget_tokens:
        for key in trim_keys:
            if excess_tokens() <= 0:
                break
            value = data.get(key)
            if not value:
                continue
            if isinstance(value, dict):
                dropped = _drop_unprotected(data, key, value, keep, excess_tokens)
                if dropped:
                    result.dropped[key] = dropped
            elif isinstance(value, str):
                _cut_text(data, key, value, excess_tokens)
            else:
                del data[key]
            if data.get(key) != value:
                result.trimmed.append(key)

        if excess_tokens() > 0:
            _excerpt_protected(data, trim_keys, keep, excess_tokens, result)

    result.tokens_after = estimate_tokens(render(data))
    result.still_over = result.tokens_after > budget_tokens
    return result


def excerpt(content: str, max_chars: int) -> str:
    """*content* cut to about *max_chars* characters: head, marker, tail.

    The marker names the number of UTF-8 bytes left out.  Content that
    already fits is returned unchanged.
    """
    if len(content) <= max_chars:
        return content
    room = max(max_chars - len(FILE_EXCERPT_MARKER.format(len(content.encode("utf-8")))), 2)
    head = content[: (room + 1) // 2]
    tail = content[len(content) - room // 2 :]
    cut = content[len(head) : len(content) - len(tail)]
    return head + FILE_EXCERPT_MARKER.format(len(cut.encode("utf-8"))) + tail


def _norm(path: str) -> str:
    """A path as the agents' file dicts key it (posix, no leading ``./``)."""
    path = str(path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _drop_unprotected(
    data: dict[str, Any],
    key: str,
    entries: dict[str, Any],
    protected: set[str],
    excess_tokens: Callable[[], int],
) -> list[str]:
    """Remove unprotected entries of ``data[key]``, last first, until the input fits."""
    remaining = dict(entries)
    dropped: list[str] = []
    for name in reversed(list(entries)):
        if excess_tokens() <= 0:
            break
        if _norm(name) in protected:
            continue
        del remaining[name]
        dropped.append(name)
        data[key] = remaining
    return dropped


def _excerpt_protected(
    data: dict[str, Any],
    trim_keys: Sequence[str],
    protected: set[str],
    excess_tokens: Callable[[], int],
    result: FitResult,
) -> None:
    """Shorten the protected files of the dict-valued *trim_keys*, largest first."""
    candidates = [
        (key, name)
        for key in trim_keys
        if isinstance(data.get(key), dict)
        for name, content in data[key].items()
        if _norm(name) in protected and isinstance(content, str)
    ]
    candidates.sort(key=lambda item: -len(data[item[0]][item[1]]))
    for key, name in candidates:
        excess = excess_tokens()
        if excess <= 0:
            return
        content = data[key][name]
        target = max(len(content) - math.ceil(excess * CHARS_PER_TOKEN), MIN_EXCERPT_CHARS)
        shortened = excerpt(content, target)
        if len(shortened) >= len(content):
            continue
        data[key] = {**data[key], name: shortened}
        result.excerpted.setdefault(key, []).append(name)
        if key not in result.trimmed:
            result.trimmed.append(key)


def _cut_text(
    data: dict[str, Any],
    key: str,
    text: str,
    excess_tokens: Callable[[], int],
) -> None:
    """Cut ``data[key]`` from the end until the input fits, or drop it."""
    keep = len(text)
    while (excess := excess_tokens()) > 0:
        keep -= math.ceil(excess * CHARS_PER_TOKEN) + len(_TRUNCATION_MARKER)
        if keep <= 0:
            del data[key]
            return
        data[key] = text[:keep] + _TRUNCATION_MARKER
