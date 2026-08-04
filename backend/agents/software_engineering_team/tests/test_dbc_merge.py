"""Unit tests for the DbC Comments agent's insertion-merge logic (merge.py)."""

from __future__ import annotations

from software_engineering_team.technical_writers.dbc_comments_agent import merge as merge_mod
from software_engineering_team.technical_writers.dbc_comments_agent.merge import (
    _resolve_original_files,
    apply_dbc_insertions,
)
from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentInsertion,
)


def _ins(**kwargs) -> DbcCommentInsertion:
    defaults = {"file": "a.py", "symbol": "foo", "comment": "Does a thing."}
    defaults.update(kwargs)
    return DbcCommentInsertion(**defaults)


def test_merge_adds_new_docstring_to_function_without_one() -> None:
    code = "### a.py ###\ndef foo():\n    return 1\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="foo", comment="Adds one.")]
    )
    assert rejected == []
    assert added == 1
    assert updated == 0
    assert '"""' in files["a.py"]
    assert "Adds one." in files["a.py"]
    assert "return 1" in files["a.py"]


def test_merge_one_liner_def_body_rejected_without_corrupting() -> None:
    """A one-liner def (body on the same physical line as the header) can't be
    anchored -- rejected explicitly rather than emitted as a line the
    post-merge syntax check would only catch downstream."""
    code = "### a.py ###\ndef foo(): return 1\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="foo", comment="Adds one.")]
    )
    assert files == {}
    assert added == 0
    assert updated == 0
    assert len(rejected) == 1
    assert "one-liner" in rejected[0]
    assert "def foo(): return 1" in code  # sanity: original untouched by this test


def test_merge_one_liner_def_existing_docstring_rejected_without_corrupting() -> None:
    code = '### a.py ###\ndef foo(): """Old."""\n'
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="foo", comment="New doc.")]
    )
    assert files == {}
    assert added == 0
    assert updated == 0
    assert len(rejected) == 1
    assert "one-liner" in rejected[0]


def test_merge_updates_existing_docstring() -> None:
    code = '### a.py ###\ndef foo():\n    """Old doc."""\n    return 1\n'
    files, added, updated, rejected = apply_dbc_insertions(
        code,
        [_ins(symbol="foo", comment="New doc.\n\nPostconditions:\n    - Returns 1.")],
    )
    assert rejected == []
    assert added == 0
    assert updated == 1
    merged = files["a.py"]
    assert "Old doc." not in merged
    assert "New doc." in merged
    assert "Returns 1." in merged
    assert "return 1" in merged


def test_merge_adds_new_docstring_to_class_without_one() -> None:
    code = "### a.py ###\nclass Foo:\n    pass\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="Foo", comment="A sample class.")]
    )
    assert rejected == []
    assert added == 1
    assert updated == 0
    assert '"""' in files["a.py"]
    assert "A sample class." in files["a.py"]
    assert "class Foo:" in files["a.py"]


def test_merge_updates_existing_class_docstring() -> None:
    code = '### a.py ###\nclass Foo:\n    """Old class doc."""\n    pass\n'
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="Foo", comment="New class doc.")]
    )
    assert rejected == []
    assert added == 0
    assert updated == 1
    merged = files["a.py"]
    assert "Old class doc." not in merged
    assert "New class doc." in merged
    assert "class Foo:" in merged


def test_merge_module_docstring_into_empty_file() -> None:
    """A truly empty (or whitespace-only) module has no first statement to
    anchor against via the usual body[0] path, but the module itself still
    exists and a docstring can be inserted at the top -- this must not be
    confused with the empty-body rejection that applies to function/class
    targets."""
    code = "### src/empty.py ###\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(file="src/empty.py", symbol="module", comment="Module docstring.")]
    )
    assert rejected == []
    assert added == 1
    assert updated == 0
    assert files["src/empty.py"].startswith('"""')
    assert "Module docstring." in files["src/empty.py"]


def test_merge_module_docstring_into_comment_only_file() -> None:
    code = "### src/comments.py ###\n# just a comment\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(file="src/comments.py", symbol="module", comment="Module docstring.")]
    )
    assert rejected == []
    assert added == 1
    merged = files["src/comments.py"]
    assert merged.startswith('"""')
    assert "Module docstring." in merged
    assert "# just a comment" in merged


def test_merge_function_with_empty_body_still_rejected(monkeypatch) -> None:
    """The empty-body guard narrowed to exclude the module target must still
    reject a function/class target whose body is empty. Valid Python can't
    actually produce an empty function/class body (ast.parse requires at
    least one statement), so this exercises the defensive branch directly by
    forcing the symbol resolver to return a body-less stand-in."""

    class _FakeTarget:
        lineno = 1
        body: list = []

    monkeypatch.setattr(merge_mod, "_find_python_target", lambda tree, symbol, line: _FakeTarget())

    files, added, updated, rejected = apply_dbc_insertions(
        "### a.py ###\ndef foo(): pass\n", [_ins(symbol="foo", comment="c")]
    )
    assert files == {}
    assert added == 0
    assert len(rejected) == 1
    assert "empty body" in rejected[0]


def test_merge_module_docstring_via_symbol_alias() -> None:
    code = "### a.py ###\nimport os\n\ndef foo():\n    pass\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="", comment="Module docstring text.")]
    )
    assert rejected == []
    assert added == 1
    merged = files["a.py"]
    assert merged.startswith('"""')
    assert "Module docstring text." in merged
    assert "import os" in merged


def test_merge_ambiguous_symbol_resolved_by_line() -> None:
    code = (
        "### a.py ###\n"
        "class A:\n"  # line 1
        "    def helper(self):\n"  # line 2
        "        pass\n"  # line 3
        "\n"  # line 4
        "class B:\n"  # line 5
        "    def helper(self):\n"  # line 6
        "        return 1\n"  # line 7
    )
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="helper", line=6, comment="B's helper.")]
    )
    assert rejected == []
    assert added == 1
    merged = files["a.py"]
    assert "B's helper." in merged
    # A's helper is untouched -- no docstring was inserted there.
    a_section, b_section = merged.split("class B:")
    assert "B's helper." not in a_section
    assert "return 1" in b_section


def test_merge_ambiguous_symbol_unresolvable_without_line() -> None:
    code = (
        "### a.py ###\n"
        "class A:\n"
        "    def helper(self):\n"
        "        pass\n"
        "\n"
        "class B:\n"
        "    def helper(self):\n"
        "        return 1\n"
    )
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="helper", line=None, comment="Which one?")]
    )
    assert files == {}
    assert added == 0
    assert updated == 0
    assert len(rejected) == 1
    assert "ambiguous" in rejected[0] or "not found" in rejected[0]


def test_merge_unknown_file_rejected() -> None:
    code = "### known.py ###\ndef f():\n    pass\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(file="missing.py", symbol="f", comment="c")]
    )
    assert files == {}
    assert added == 0
    assert updated == 0
    assert len(rejected) == 1
    assert "unknown file" in rejected[0]


def test_merge_non_python_valid_line_anchor_renders_block_comment() -> None:
    code = "### a.ts ###\nexport function add(a, b) {\n  return a + b;\n}\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(file="a.ts", symbol="add", line=1, comment="Adds a and b.")]
    )
    assert rejected == []
    assert added == 1
    assert updated == 0
    merged = files["a.ts"]
    assert "/**" in merged
    assert "* Adds a and b." in merged
    assert "*/" in merged
    assert "export function add(a, b) {" in merged


def test_merge_non_python_preformatted_jsdoc_not_doubled() -> None:
    """A model returning an already-formatted /** ... */ block (a common,
    reasonable output) must not get its own '* ' markers doubled."""
    code = "### a.ts ###\nexport function add(a, b) {\n  return a + b;\n}\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code,
        [_ins(file="a.ts", symbol="add", line=1, comment="/**\n * Adds a and b.\n */")],
    )
    assert rejected == []
    merged = files["a.ts"]
    assert "* * Adds a and b." not in merged
    assert "* Adds a and b." in merged
    assert merged.count("/**") == 1
    assert merged.count("*/") == 1


def test_merge_non_python_preformatted_jsdoc_multiline_preserved() -> None:
    code = "### a.ts ###\nexport function add(a, b) {\n  return a + b;\n}\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code,
        [
            _ins(
                file="a.ts",
                symbol="add",
                line=1,
                comment="/**\n * Line one.\n * Line two.\n */",
            )
        ],
    )
    assert rejected == []
    merged = files["a.ts"]
    assert "* Line one." in merged
    assert "* Line two." in merged
    assert "* * Line one." not in merged
    assert merged.count("/**") == 1


def test_merge_non_python_out_of_range_line_rejected() -> None:
    code = "### a.ts ###\nexport function add(a, b) {\n  return a + b;\n}\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(file="a.ts", symbol="add", line=999, comment="c")]
    )
    assert files == {}
    assert len(rejected) == 1
    assert "no valid line anchor" in rejected[0]


def test_merge_duplicate_python_insertion_rejected() -> None:
    code = "### a.py ###\ndef foo():\n    return 1\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code,
        [
            _ins(symbol="foo", comment="First."),
            _ins(symbol="foo", comment="Second."),
        ],
    )
    assert added == 1
    assert "First." in files["a.py"]
    assert "Second." not in files["a.py"]
    assert len(rejected) == 1
    assert "duplicate insertion" in rejected[0]


def test_merge_multiple_insertions_apply_bottom_up_without_corrupting_offsets() -> None:
    code = "### a.py ###\ndef a():\n    pass\n\n\ndef b():\n    pass\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code,
        [
            _ins(symbol="a", comment="Does a."),
            _ins(symbol="b", comment="Does b."),
        ],
    )
    assert rejected == []
    assert added == 2
    merged = files["a.py"]
    assert "Does a." in merged
    assert "Does b." in merged
    a_idx = merged.index("Does a.")
    b_idx = merged.index("Does b.")
    assert a_idx < b_idx
    assert "def a():" in merged
    assert "def b():" in merged


def test_merge_quote_collision_rejected_without_corrupting() -> None:
    code = "### a.py ###\ndef foo():\n    return 1\n"
    bad_comment = "Has both " + '"""' + " and " + "'''" + " inside."
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="foo", comment=bad_comment)]
    )
    assert files == {}
    assert added == 0
    assert len(rejected) == 1
    assert "quote" in rejected[0]


def test_merge_post_merge_syntax_check_rejection(monkeypatch) -> None:
    """Even a structurally-successful merge is dropped if the post-merge safety
    check (reject_invalid_python) flags it -- forced here via monkeypatch since
    the real anchoring logic can't produce invalid syntax on its own."""

    def _force_invalid(files):
        return {}, {path: "forced failure" for path in files}

    monkeypatch.setattr(merge_mod, "reject_invalid_python", _force_invalid)

    code = "### a.py ###\ndef foo():\n    return 1\n"
    files, added, updated, rejected = apply_dbc_insertions(
        code, [_ins(symbol="foo", comment="Adds one.")]
    )
    assert files == {}
    assert added == 0
    assert updated == 0
    assert len(rejected) == 1
    assert "post-merge syntax check" in rejected[0]


def test_resolve_original_files_multi_file_headers() -> None:
    code = "### a.py ###\ndef f():\n    pass\n### b.py ###\ndef g():\n    pass\n"
    result = _resolve_original_files(code, [_ins(file="a.py"), _ins(file="b.py")])
    assert set(result.keys()) == {"a.py", "b.py"}
    assert "def f():" in result["a.py"]
    assert "def g():" in result["b.py"]


def test_resolve_original_files_single_headerless_blob() -> None:
    code = "def f():\n    pass\n"
    result = _resolve_original_files(code, [_ins(file="x.py", symbol="f")])
    assert result == {"x.py": code.strip()}


def test_resolve_original_files_headerless_blob_ambiguous_target_returns_empty() -> None:
    code = "def f():\n    pass\n"
    result = _resolve_original_files(
        code, [_ins(file="x.py", symbol="f"), _ins(file="y.py", symbol="g")]
    )
    assert result == {}
