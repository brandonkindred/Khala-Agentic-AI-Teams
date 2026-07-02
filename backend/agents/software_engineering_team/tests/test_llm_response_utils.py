"""Tests for ``shared_llm_recovery.recovery``.

Covers JSON extraction from LLM-wrapped content, markdown code-block file
parsing, and heuristic recovery helpers.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# extract_task_assignment_from_content
# ---------------------------------------------------------------------------


def test_extract_task_assignment_empty() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    assert extract_task_assignment_from_content("") is None
    assert extract_task_assignment_from_content("   ") is None


def test_extract_task_assignment_no_brace() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    assert extract_task_assignment_from_content("no JSON here") is None


def test_extract_task_assignment_finds_inline_json() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    text = """
    Here is the plan:
    {"tasks": [{"id": "t1"}], "execution_order": ["t1"]}
    Done.
    """
    out = extract_task_assignment_from_content(text)
    assert out is not None
    assert out["tasks"] == [{"id": "t1"}]


def test_extract_task_assignment_with_thinking_blocks() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    text = '<think>reasoning</think>\n<thinking>more</thinking>\n<reasoning>hmm</reasoning>\n{"tasks":[{"id":"a"}]}'
    out = extract_task_assignment_from_content(text)
    assert out is not None
    assert out["tasks"]


def test_extract_task_assignment_xml_json_tag() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    text = '<json>{"tasks": [{"id": "a"}]}</json>'
    out = extract_task_assignment_from_content(text)
    assert out is not None


def test_extract_task_assignment_from_markdown_code_block() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    # First inline match has no tasks (empty {}), should fall through to code block
    text2 = '{ } \n```json\n{"tasks":[{"id":"x"}]}\n```'
    out = extract_task_assignment_from_content(text2)
    assert out is not None
    assert out["tasks"][0]["id"] == "x"


def test_extract_task_assignment_no_tasks_field() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    # JSON exists but no tasks key — returns None
    out = extract_task_assignment_from_content('{"other": [1,2]}')
    assert out is None


def test_extract_task_assignment_malformed_braces() -> None:
    from shared_llm_recovery.recovery import (
        extract_task_assignment_from_content,
    )

    # Unbalanced braces with no closing — also no valid task
    text = "{ broken"
    assert extract_task_assignment_from_content(text) is None


# ---------------------------------------------------------------------------
# _looks_like_path
# ---------------------------------------------------------------------------


def test_looks_like_path_basics() -> None:
    from shared_llm_recovery.recovery import _looks_like_path

    assert _looks_like_path("src/foo.ts") is True
    assert _looks_like_path("foo.py") is True
    assert _looks_like_path("README.md") is True
    assert _looks_like_path("") is False
    assert _looks_like_path("just text") is False
    assert _looks_like_path("x" * 250) is False


# ---------------------------------------------------------------------------
# extract_files_from_content
# ---------------------------------------------------------------------------


def test_extract_files_empty() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    assert extract_files_from_content("") == {}


def test_extract_files_from_json_with_files_key() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = '{"files": {"a.py": "print(1)", "b.py": "print(2)"}}'
    out = extract_files_from_content(content)
    assert out == {"a.py": "print(1)", "b.py": "print(2)"}


def test_extract_files_skips_invalid_entries() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = '{"files": {"a.py": "code", "bad": "", "x": 123}}'
    out = extract_files_from_content(content)
    assert out == {"a.py": "code"}


def test_extract_files_json_in_markdown() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = """
```json
{"files": {"src/foo.ts": "export {}"}}
```
"""
    out = extract_files_from_content(content)
    assert out == {"src/foo.ts": "export {}"}


def test_extract_files_path_in_fence_info() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = """
```src/foo.py
def x(): pass
```
"""
    out = extract_files_from_content(content)
    assert "src/foo.py" in out


def test_extract_files_path_as_first_line() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = """
```
src/bar.ts
const x = 1;
```
"""
    out = extract_files_from_content(content)
    assert "src/bar.ts" in out
    assert "const x = 1;" in out["src/bar.ts"]


def test_extract_files_first_line_looks_like_path() -> None:
    """Confirm the ``_looks_like_path(lines[0])`` branch where the body's
    first line is itself a path-like token."""
    from shared_llm_recovery.recovery import extract_files_from_content

    content = "```python\nsrc/main.py\ndef hi(): pass\n```"
    out = extract_files_from_content(content)
    assert "src/main.py" in out


def test_extract_files_returns_empty_on_no_match() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    out = extract_files_from_content("just plain text, no fences, no json")
    assert out == {}


def test_extract_files_no_path_in_block_returns_empty() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = """
```python
def x(): pass
```
"""
    out = extract_files_from_content(content)
    assert out == {}


def test_extract_files_handles_invalid_json_gracefully() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = "{ not json at all }"
    # Falls through to code-block patterns; nothing found
    out = extract_files_from_content(content)
    assert out == {}


def test_extract_files_extension_as_info_only() -> None:
    from shared_llm_recovery.recovery import extract_files_from_content

    content = "```foo.py\nbody\n```"
    out = extract_files_from_content(content)
    assert "foo.py" in out


# ---------------------------------------------------------------------------
# heuristic_extract_files_from_content
# ---------------------------------------------------------------------------


def test_heuristic_extract_empty() -> None:
    from shared_llm_recovery.recovery import (
        heuristic_extract_files_from_content,
    )

    assert heuristic_extract_files_from_content("") == {}
    assert heuristic_extract_files_from_content("   ") == {}


def test_heuristic_extract_from_markdown_header() -> None:
    from shared_llm_recovery.recovery import (
        heuristic_extract_files_from_content,
    )

    content = """
## src/app.py

def hello():
    return "world"


## src/util.ts

export const x = 1;
const y = 2;
"""
    out = heuristic_extract_files_from_content(content)
    assert "src/app.py" in out
    assert "src/util.ts" in out


def test_heuristic_extract_from_file_label() -> None:
    from shared_llm_recovery.recovery import (
        heuristic_extract_files_from_content,
    )

    content = """
File: src/foo.py

def x():
    return 42
def y():
    return 0
"""
    out = heuristic_extract_files_from_content(content)
    assert "src/foo.py" in out


def test_heuristic_extract_from_standalone_path_line() -> None:
    from shared_llm_recovery.recovery import (
        heuristic_extract_files_from_content,
    )

    content = """
src/widget.ts
export const widget = 1;
const x = 2;
"""
    out = heuristic_extract_files_from_content(content)
    assert "src/widget.ts" in out


def test_heuristic_extract_ignores_short_content() -> None:
    from shared_llm_recovery.recovery import (
        heuristic_extract_files_from_content,
    )

    # Very short bodies (< 10 chars) are dropped
    content = """
src/a.py

short
"""
    out = heuristic_extract_files_from_content(content)
    assert "src/a.py" not in out


# ---------------------------------------------------------------------------
# extract_single_python_block
# ---------------------------------------------------------------------------


def test_extract_single_python_block_found() -> None:
    from shared_llm_recovery.recovery import (
        extract_single_python_block,
    )

    content = """
Some text.
```python
def hello():
    return "world"

def goodbye():
    return "see ya"
```
More text.
"""
    out = extract_single_python_block(content)
    assert out is not None
    assert "def hello" in out


def test_extract_single_python_block_py_alias() -> None:
    from shared_llm_recovery.recovery import (
        extract_single_python_block,
    )

    content = "```py\nimport sys\nfrom pathlib import Path\n```"
    out = extract_single_python_block(content)
    assert out is not None
    assert "import sys" in out


def test_extract_single_python_block_empty() -> None:
    from shared_llm_recovery.recovery import (
        extract_single_python_block,
    )

    assert extract_single_python_block("") is None
    assert extract_single_python_block("no code blocks") is None


def test_extract_single_python_block_too_short() -> None:
    from shared_llm_recovery.recovery import (
        extract_single_python_block,
    )

    # Body under 20 chars is rejected
    content = "```python\nx=1\n```"
    assert extract_single_python_block(content) is None
