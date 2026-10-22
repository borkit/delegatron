"""Tests for the markdown helpers behind agent user messages."""

from delegatron.agents.rendering import (
    bullet_list,
    fenced,
    files_block,
    json_block,
    report_excerpt,
)


def test_fenced_uses_three_backticks_by_default():
    assert fenced("print('hi')\n", "python") == "```python\nprint('hi')\n```"


def test_fenced_outgrows_backtick_runs_in_text():
    text = "Run:\n````bash\nmake\n````"

    assert fenced(text) == f"`````\n{text}\n`````"


def test_json_block_keeps_non_ascii_readable():
    assert json_block({"title": "Pokémon deal"}) == '```json\n{\n  "title": "Pokémon deal"\n}\n```'


def test_files_block_renders_one_section_per_file():
    block = files_block({"a.py": "A = 1\n", "b.py": "B = 2\n"})

    assert block == "### a.py\n```\nA = 1\n```\n\n### b.py\n```\nB = 2\n```"


def test_bullet_list():
    assert bullet_list(["a", "b"]) == "- a\n- b"


def test_report_excerpt_prefers_sandbox_excerpt():
    report = {"failure_excerpt": "E   AssertionError", "stdout": "truncated", "stderr": "noise"}

    assert report_excerpt(report) == "E   AssertionError"


def test_report_excerpt_falls_back_to_stdout_and_stderr():
    report = {"passed": False, "stdout": "collected 0 items", "stderr": "Parse-check gate failed"}

    assert report_excerpt(report) == "collected 0 items\nParse-check gate failed"


def test_report_excerpt_empty_report():
    assert report_excerpt({"passed": False}) == ""
