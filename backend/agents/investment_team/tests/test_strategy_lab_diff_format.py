"""Unit coverage for :func:`diff_or_full` (compact code-diff formatting).

Covers the three acceptance-criteria cases from the diff-formatting-utility
issue: no previous round (full text), a small incremental change (diff),
and a near-total-rewrite (falls back to full text).
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


def test_identical_code_returns_diff_not_full_text():
    code = "def f():\n    return 42\n"

    result = diff_or_full(code, code)

    assert result != code
    assert len(result) < len(code)


def test_empty_previous_code_is_not_none_and_diffs():
    previous = ""
    current = "x = 1\n"

    result = diff_or_full(previous, current)

    assert result == current or "x = 1" in result
