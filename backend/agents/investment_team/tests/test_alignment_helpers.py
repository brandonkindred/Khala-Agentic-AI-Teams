"""Coverage for the format/coerce helpers in
``strategy_lab.agents.alignment``.
"""

from __future__ import annotations

import pytest

from investment_team.strategy_lab.agents.alignment import (
    _coerce_report,
    _extract_json,
    _parse_legitimate,
)

# ---------------------------------------------------------------------------
# _coerce_report
# ---------------------------------------------------------------------------


def test_coerce_report_aligned_clears_proposed_code() -> None:
    report = _coerce_report(
        {
            "aligned": True,
            "rationale": "trades match the spec",
            "issues": [],
            "proposed_code": "def x(): pass",  # ignored when aligned=True
            "predicted_aligned_after_fix": True,
            "changes_made": "tightened entry",
        },
        fallback_code="def fallback(): pass",
    )
    assert report.aligned is True
    assert report.proposed_code is None
    assert report.predicted_aligned_after_fix is False
    assert report.changes_made == ""


def test_coerce_report_misaligned_with_no_proposal_sets_predicted_false() -> None:
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "trades drifted",
            "issues": [{"rule_type": "entry_rules", "description": "..."}],
            "proposed_code": None,
            "predicted_aligned_after_fix": True,
        },
        fallback_code="def x(): pass",
    )
    assert report.aligned is False
    assert report.proposed_code is None
    # Auto-cleared because there's nothing to act on.
    assert report.predicted_aligned_after_fix is False


def test_coerce_report_normalises_bad_issue_to_warning() -> None:
    """A non-dict issue is skipped; a dict with an unknown severity defaults to warning."""
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "bad",
            "issues": [
                "junk-string",  # skipped
                {"rule_type": "exit_rules", "description": "x", "severity": "wild"},
            ],
            "proposed_code": "def x(): pass",
        },
        fallback_code="def y(): pass",
    )
    assert len(report.issues) == 1
    assert report.issues[0].severity == "warning"


def test_coerce_report_passes_through_proposed_code_when_misaligned() -> None:
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "x",
            "proposed_code": "def fix(): pass",
            "predicted_aligned_after_fix": True,
            "changes_made": "y",
        },
        fallback_code="def fallback(): pass",
    )
    assert report.aligned is False
    assert report.proposed_code == "def fix(): pass"
    assert report.predicted_aligned_after_fix is True
    assert report.changes_made == "y"


def test_coerce_report_preserve_proposed_code_keeps_patch_on_aligned_true() -> None:
    """``preserve_proposed_code=True`` on the fix-proposer path keeps
    the LLM's patch even when it over-claims ``aligned=true``.

    Without this flag, an LLM that confidently returns aligned=true
    with a usable patch would dead-end the alignment loop at
    ``no_proposed_fix``, leaving the deterministic critical findings
    unrepaired. Regression for PR #613 review.
    """
    raw = {
        "aligned": True,
        "rationale": "model thinks aligned",
        "issues": [],
        "proposed_code": "def fix(): pass",
        "predicted_aligned_after_fix": True,
        "changes_made": "real fix",
    }
    report = _coerce_report(raw, fallback_code="orig", preserve_proposed_code=True)
    # Patch survives the aligned=true LLM over-claim.
    assert report.aligned is True
    assert report.proposed_code == "def fix(): pass"
    assert report.changes_made == "real fix"
    assert report.predicted_aligned_after_fix is True


def test_coerce_report_blank_proposed_code_treated_as_none() -> None:
    report = _coerce_report(
        {
            "aligned": False,
            "rationale": "x",
            "proposed_code": "   ",
        },
        fallback_code="def fallback(): pass",
    )
    assert report.proposed_code is None


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_plain_object() -> None:
    data = _extract_json('preamble {"aligned": true, "issues": []} trailing')
    assert data == {"aligned": True, "issues": []}


def test_extract_json_handles_markdown_fence() -> None:
    text = '```json\n{"aligned": true}\n```'
    data = _extract_json(text)
    assert data == {"aligned": True}


def test_extract_json_handles_nested_braces() -> None:
    text = '{"a": {"b": 1}}'
    data = _extract_json(text)
    assert data == {"a": {"b": 1}}


def test_extract_json_raises_when_no_object() -> None:
    with pytest.raises(ValueError):
        _extract_json("no braces here")


def test_extract_json_raises_on_malformed_json() -> None:
    with pytest.raises(ValueError):
        _extract_json('{"missing_closing_quote: 1}')


# ---------------------------------------------------------------------------
# _parse_legitimate — strict near-miss verdict parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("  TRUE  ", True),
        ("false", False),
        ("False", False),
        # Anything that isn't an unambiguous "yes" must fail closed
        # so a misaligned trade is never waved through by the near-miss
        # adjudicator.
        ("yes", False),
        ("1", False),
        ("0", False),
        ("", False),
        (None, False),
        (1, False),
        (0, False),
        ({"nested": True}, False),
    ],
)
def test_parse_legitimate_strict(raw, expected: bool) -> None:
    """Regression for the ``bool('false') == True`` trap. The near-miss
    parser must read only real ``bool`` or the case-insensitive string
    literals ``true`` / ``false``; everything else fails closed."""
    assert _parse_legitimate(raw) is expected
