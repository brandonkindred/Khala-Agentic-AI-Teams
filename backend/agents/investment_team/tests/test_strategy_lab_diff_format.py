"""Unit coverage for :func:`diff_or_full`.

Covers the three acceptance-criteria cases for the code-string diff utility:
no previous round (full text), a small incremental change (diff), and a
near-total-rewrite (falls back to full text).
"""

from __future__ import annotations

from investment_team.strategy_lab.agents._diff_format import diff_or_full


def test_no_previous_round_returns_full_text():
    current = "def strategy():\n    return 1\n"

    result = diff_or_full(None, current)

    assert result == current


def test_small_incremental_change_returns_diff():
    previous = "\n".join(f"line_{i} = {i}" for i in range(50)) + "\n"
    current = previous.replace("line_10 = 10", "line_10 = 999")

    result = diff_or_full(previous, current)

    assert result != current
    assert "line_10" in result
    assert "999" in result
    assert len(result) < len(current)


def test_near_total_rewrite_falls_back_to_full_text():
    previous = "\n".join(f"old_line_{i}" for i in range(30)) + "\n"
    current = "\n".join(f"totally_different_content_{i}_xyz" for i in range(30)) + "\n"

    result = diff_or_full(previous, current)

    assert result == current


def test_identical_code_returns_full_text_not_an_empty_diff():
    """A no-op round must resend the full code, never an empty diff.

    This assertion is deliberately inverted from the one this test carried
    when the utility was introduced (#7409), where it pinned the raw length
    rule while ``diff_or_full`` was still standalone and "not yet wired into
    refinement.py's prompt path" — an empty return had no consumer and so no
    consequence. #7424 wired it in, which turned this case into a live defect:

    ``unified_diff`` yields nothing for identical inputs, and
    ``len("") < len(current_code)`` holds for any non-empty code, so the old
    behavior returned ``""``. ``RefinementAgent.run`` then renders the diff
    section as an explanatory preamble around an empty fence — a fixed ~176
    characters against ``len(code) + 17`` for the full section — and, keeping
    whichever section is shorter, *selects* it for any strategy file longer
    than roughly 150 characters. The model received "reconstruct the current
    file from context, then respond with the complete fixed file" with no code
    in the prompt and no conversation history to recover it from.
    """
    code = "def f():\n    return 42\n"

    result = diff_or_full(code, code)

    assert result == code


def test_empty_previous_code_is_not_none_and_diffs():
    previous = ""
    current = "x = 1\n"

    result = diff_or_full(previous, current)

    assert result == current or "x = 1" in result


def test_no_trailing_newline_change_does_not_concatenate_lines():
    previous = "\n".join(f"line_{i} = {i}" for i in range(50))
    current = previous.replace("line_49 = 49", "line_49 = 999")

    result = diff_or_full(previous, current)

    assert "line_49 = 49\n" in result
    assert "999" in result
    assert "49999" not in result
    assert "49+line_49" not in result
