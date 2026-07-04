"""Tests for the ``repair`` and ``repair_truncated`` gates on the salvage engine.

These pin the shared salvage behaviour the two consolidated callers rely on:
the strategy-lab wrapper runs ``extract_json_object(..., repair=False)`` so a
malformed/truncated payload yields ``None`` (its wrapper then re-prompts), and
the Ollama client runs ``repair_truncated=False`` so a genuinely truncated reply
yields ``None`` (its caller then continues) while complete-but-broken JSON is
still repaired.
"""

from __future__ import annotations

from shared_llm_recovery import extract_json_object

# ---------------------------------------------------------------------------
# repair=False (strict): only strictly valid JSON survives; no json-repair.
# ---------------------------------------------------------------------------


def test_strict_parses_clean_object() -> None:
    assert extract_json_object('{"ready": true, "issues": []}', repair=False) == {
        "ready": True,
        "issues": [],
    }


def test_strict_parses_prose_wrapped_object() -> None:
    assert extract_json_object('preamble {"a": 1, "b": {"c": 2}} trailer', repair=False) == {
        "a": 1,
        "b": {"c": 2},
    }


def test_strict_parses_object_with_braces_inside_string_value() -> None:
    """The string-aware scanner is retained in strict mode — a brace inside a
    string value must not truncate the object."""
    assert extract_json_object(
        '{"rationale": "close > {threshold}", "aligned": false}', repair=False
    ) == {"rationale": "close > {threshold}", "aligned": False}


def test_strict_parses_markdown_fenced_object() -> None:
    assert extract_json_object('```json\n{"ok": 1}\n```', repair=False) == {"ok": 1}


def test_strict_returns_none_for_no_json() -> None:
    assert extract_json_object("just prose, no JSON at all", repair=False) is None


def test_strict_returns_none_for_invalid_substring() -> None:
    """A ``{...}`` that isn't valid JSON is NOT repaired in strict mode."""
    assert extract_json_object('{"ready": this-is-not-valid}', repair=False) is None


def test_strict_returns_none_for_unterminated_string() -> None:
    assert extract_json_object('{"k": "unterminated }', repair=False) is None


def test_strict_returns_none_for_truncated_object() -> None:
    """A cut-off object (the max-tokens case) must NOT be repaired in strict
    mode — it yields ``None`` so the caller re-prompts rather than accepting a
    fabricated tail."""
    assert extract_json_object('{"strategy_code": "# half', repair=False) is None


# ---------------------------------------------------------------------------
# repair=True (default): tolerant json-repair still salvages complete-but-broken
# JSON, so the Ollama non-truncated path keeps working.
# ---------------------------------------------------------------------------


def test_repair_true_salvages_unescaped_inner_quotes() -> None:
    """A complete object with unescaped inner quotes is repaired (json-repair);
    the same input is rejected under repair=False."""
    broken = '{"summary": "displays "Resource": "*" wrong", "ok": true}'
    assert extract_json_object(broken, repair=False) is None
    repaired = extract_json_object(broken, repair=True)
    assert isinstance(repaired, dict)
    assert "summary" in repaired


def test_repair_true_still_returns_none_for_no_json() -> None:
    assert extract_json_object("no braces here", repair=True) is None


# ---------------------------------------------------------------------------
# repair_truncated=False (the Ollama mode): repair complete-but-broken JSON,
# but let a genuinely truncated reply yield None so the caller can continue.
# ---------------------------------------------------------------------------


def test_repair_truncated_false_still_repairs_trailing_comma() -> None:
    """A COMPLETE object needing only trailing-comma repair is still salvaged —
    even when its string value carries an unbalanced bracket (a brace-count
    heuristic would wrongly call this truncated and refuse to repair it)."""
    out = extract_json_object('{"regex": "[a-z", "ok": true,}', repair_truncated=False)
    assert out == {"regex": "[a-z", "ok": True}


def test_repair_truncated_false_returns_none_for_truncated_object() -> None:
    """A never-closed (max-tokens-truncated) object yields None so the caller
    continues rather than accepting a fabricated tail."""
    assert (
        extract_json_object('{"files": {"a.py": "def f():  # incomplete', repair_truncated=False)
        is None
    )


def test_repair_truncated_false_returns_none_for_prose_prefixed_truncation() -> None:
    """A truncated object behind a prose/fence prefix must also yield None — the
    engine sees the real payload boundaries, so the prefix cannot smuggle a
    fabricated tail past the guard the way a startswith('{') heuristic did."""
    assert (
        extract_json_object('Here is:\n{"files": {"a.py": "x  # incomplete', repair_truncated=False)
        is None
    )
    assert (
        extract_json_object('```json\n{"files": {"a.py": "x  # incomplete', repair_truncated=False)
        is None
    )


def test_repair_truncated_true_default_still_repairs_truncation() -> None:
    """The default (repair_truncated=True) preserves the original tolerant
    behaviour for callers that want a fabricated close (SE / coding teams)."""
    out = extract_json_object('{"files": {"a.py": "x  # incomplete', repair_truncated=True)
    assert out == {"files": {"a.py": "x  # incomplete"}}


def test_repair_false_overrides_repair_truncated() -> None:
    """repair=False disables all repair regardless of repair_truncated."""
    assert extract_json_object('{"a": 1,}', repair=False, repair_truncated=True) is None
