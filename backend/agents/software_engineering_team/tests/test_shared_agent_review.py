"""Tests for software_engineering_team.shared.agent_review.

These exercise the shared QA/security review orchestration directly (the two V2
team wrappers delegate to it). The team-specific ``ReviewIssue`` type is injected,
so the helper is tested in isolation from any one team's models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from unittest.mock import MagicMock

from software_engineering_team.shared.agent_review import (
    run_chunked_agent_review,
    run_qa_agent,
    run_security_agent,
)

MAX = 60_000


@dataclass
class _Issue:
    source: str = ""
    severity: str = "medium"
    description: str = ""
    file_path: str = ""
    recommendation: str = ""


class _Bug:
    severity = "low"
    description = "real bug"
    location = "big.py"
    recommendation = ""


def _big_source() -> str:
    """A multi-function file larger than one prompt budget.

    Grows whole ``def fn_*`` functions until the source exceeds MAX, so cuts at
    function boundaries are exercised; begins with fn_0000 and ends with fn_tail.
    """
    lines: list[str] = []
    total = 0
    i = 0
    while total <= MAX:
        fn = f"def fn_{i:04d}():\n    return {i}"
        lines.append(fn)
        total += len(fn) + 1
        i += 1
    lines.append("def fn_tail():\n    return -1")
    big = "\n".join(lines)
    assert len(big) > MAX
    return big


def _is_raw(piece: str) -> bool:
    """True when a piece carries neither a ### path ### header nor N: prefixes."""
    return "### " not in piece and re.search(r"(?m)^\d+: ", piece) is None


def test_function_aware_split_feeds_raw_source():
    """A too-large multi-function file is split at function boundaries and each
    piece is RAW source (no ### header, no N: line prefixes), head and tail kept."""
    codes: list[str] = []

    def run_chunk(code: str):
        codes.append(code)
        return [_Bug()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"big.py": _big_source()},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    assert len(codes) > 1  # split into multiple function-aware pieces
    assert all(len(c) <= MAX for c in codes)  # every piece within budget
    assert all(_is_raw(c) for c in codes)  # raw source, not reviewer-rendered
    assert all(c.lstrip().startswith("def ") for c in codes)  # cuts at function boundaries
    joined = "\n".join(codes)
    assert "fn_0000" in joined and "fn_tail" in joined  # head and tail both reviewed
    assert len(issues) == len(codes)
    assert all(i.source == "qa" for i in issues)


def test_oversized_single_line_hard_splits_raw():
    """A single line longer than the cap (a minified bundle) has no function
    boundary, so it falls back to a character-bounded hard-split — still raw."""
    codes: list[str] = []

    def run_chunk(code: str):
        codes.append(code)
        return []

    line = "DATA = '" + ("a" * (MAX + 5_000)) + "'"
    assert "\n" not in line and len(line) > MAX

    run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"bundle.py": line},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )

    assert len(codes) > 1
    assert all(len(c) <= MAX for c in codes)
    assert all(_is_raw(c) for c in codes)
    assert "".join(codes) == line  # whole line reconstructs, nothing dropped


def test_small_file_single_call():
    """A file that fits is reviewed in one call (no regression)."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        assert code == "def f():\n    return 1"  # raw, whole file
        return []

    run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"x.py": "def f():\n    return 1"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert calls["n"] == 1


def test_blank_files_trigger_no_call():
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        return []

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"empty.py": "   \n\t"},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert issues == [] and calls["n"] == 0


def test_failing_piece_skipped_others_survive():
    """A piece whose run_chunk raises is skipped; the others' issues survive."""
    calls = {"n": 0}

    def run_chunk(code: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("agent unavailable")
        return [_Bug()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"big.py": _big_source()},
        source="qa",
        default_severity="medium",
        label="QA agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=0,  # exercise the many-pieces warning path
    )
    assert calls["n"] > 1
    assert len(issues) >= 1
    assert all(i.description == "real bug" for i in issues)


def test_file_path_defaults_to_sent_file_when_agent_gives_none():
    """When the agent reports location=None, the finding is attributed to the
    file actually sent rather than left blank."""

    class _NoLoc:
        severity = "high"
        description = "x"
        location = None
        recommendation = ""

    def run_chunk(code: str):
        return [_NoLoc()]

    issues = run_chunked_agent_review(
        run_chunk=run_chunk,
        files={"svc.py": "def f():\n    return 1"},
        source="security",
        default_severity="high",
        label="Security agent",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert len(issues) == 1
    assert issues[0].file_path == "svc.py"


def test_run_qa_agent_builds_qa_input_and_extracts_bugs():
    """run_qa_agent feeds raw code to the QA agent and maps bugs_found to issues."""
    codes: list[str] = []

    class _QAAgent:
        def run(self, inp):
            codes.append(inp.code)
            assert inp.language == "python"
            return MagicMock(bugs_found=[_Bug()])

    issues = run_qa_agent(
        qa_agent=_QAAgent(),
        files={"x.py": "def f():\n    return 1"},
        language="python",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert codes == ["def f():\n    return 1"]
    assert len(issues) == 1 and issues[0].source == "qa"


def test_run_security_agent_builds_security_input_and_extracts_vulns():
    """run_security_agent feeds raw code to the security agent and maps vulns."""
    codes: list[str] = []

    class _Vuln:
        severity = "high"
        description = "XSS"
        location = "x.ts"
        recommendation = "sanitize"

    class _SecAgent:
        def run(self, inp):
            codes.append(inp.code)
            return MagicMock(vulnerabilities=[_Vuln()])

    issues = run_security_agent(
        security_agent=_SecAgent(),
        files={"x.ts": "const f = () => 1;"},
        language="typescript",
        task_description="t",
        task_id="t1",
        issue_factory=_Issue,
        max_chars=MAX,
        warn_threshold=20,
    )
    assert codes == ["const f = () => 1;"]
    assert len(issues) == 1 and issues[0].source == "security"
