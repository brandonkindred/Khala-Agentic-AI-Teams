"""Tests for the strict ``repair`` gate and the ``looks_truncated`` heuristic.

These pin the shared salvage behaviour the two consolidated callers rely on:
the strategy-lab wrapper runs ``extract_json_object(..., repair=False)`` so a
malformed/truncated payload yields ``None`` (its wrapper then re-prompts), and
the Ollama client gates repair on ``looks_truncated`` to preserve continuation.
"""

from __future__ import annotations

from shared_llm_recovery import extract_json_object, looks_truncated

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
# looks_truncated
# ---------------------------------------------------------------------------


def test_looks_truncated_unbalanced_braces() -> None:
    assert looks_truncated('{"a": 1') is True


def test_looks_truncated_unbalanced_brackets() -> None:
    assert looks_truncated('{"a": [1, 2') is True


def test_looks_truncated_unclosed_string() -> None:
    assert looks_truncated('{"a": "unterminated') is True


def test_looks_truncated_false_for_complete_object() -> None:
    assert looks_truncated('{"a": 1, "b": [2, 3]}') is False


def test_looks_truncated_ignores_escaped_quote() -> None:
    """An escaped quote inside a closed string does not leave the scan open."""
    assert looks_truncated('{"a": "he said \\"hi\\""}') is False


def test_looks_truncated_empty_is_false() -> None:
    assert looks_truncated("") is False
    assert looks_truncated("   ") is False
