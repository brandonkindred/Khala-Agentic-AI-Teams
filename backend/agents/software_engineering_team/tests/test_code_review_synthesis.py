"""Tests for the reduce-phase findings synthesis (Stage 2).

``synthesis.py`` owns the narrative only: a single findings-only LLM pass that
merges per-chunk summaries/notes. These tests cover the digest builder (ordering
and completeness, no code), the synthesis call's success path and its ``None``
fallback on every failure mode, and the coordinator integration — synthesis is
used for multi-chunk reviews, skipped for single-chunk ones, never mutates the
verdict or issues, and falls back to concatenation when it returns ``None``.
"""

from __future__ import annotations

from typing import Any, Dict

from code_review_agent import coordinator as coordinator_mod
from code_review_agent.coordinator import run_coordinator
from code_review_agent.models import CodeReviewInput, CodeReviewIssue
from code_review_agent.synthesis import (
    SynthesisResult,
    build_findings_digest,
    synthesize_review_findings,
    synthesize_spec_compliance,
)

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars


def _issue(
    severity: str,
    description: str,
    *,
    file_path: str = "app/x.py",
    suggestion: str = "fix it",
    line: int | None = None,
) -> CodeReviewIssue:
    return CodeReviewIssue(
        severity=severity,
        category="logic",
        file_path=file_path,
        description=description,
        suggestion=suggestion,
        line=line,
    )


def _input(**kwargs: Any) -> CodeReviewInput:
    base: Dict[str, Any] = dict(code="### a.py ###\nx = 1", task_description="t", language="python")
    base.update(kwargs)
    return CodeReviewInput(**base)


# ---------------------------------------------------------------------------
# build_findings_digest — ordering & completeness, never any code
# ---------------------------------------------------------------------------


def test_digest_orders_critical_first_and_renders_everything_in_full() -> None:
    """Issues are ordered critical→info and every issue/summary is rendered in
    full — there are no length caps of any kind."""
    long_desc = "D" * 5_000
    long_summary = "S" * 5_000
    issues = [
        _issue("info", "info finding"),
        _issue("critical", long_desc),
        _issue("low", "low finding"),
        _issue("high", "high finding"),
        _issue("medium", "medium finding"),
    ]
    digest = build_findings_digest(issues, [long_summary, "second summary"])

    # Severity ordering: critical → high → medium → low → info.
    assert (
        digest.index("[critical]")
        < digest.index("[high]")
        < digest.index("[medium]")
        < digest.index("[low]")
        < digest.index("[info]")
    )
    # Completeness: long strings survive in full (no truncation), every summary present.
    assert long_desc in digest
    assert long_summary in digest
    assert "second summary" in digest


def test_digest_is_stable_within_a_severity() -> None:
    """Equal severities keep their input order (stable sort)."""
    issues = [
        _issue("high", "first high"),
        _issue("high", "second high"),
        _issue("high", "third high"),
    ]
    digest = build_findings_digest(issues, [])
    assert digest.index("first high") < digest.index("second high") < digest.index("third high")


def test_digest_unknown_severity_sorts_after_known_and_survives() -> None:
    issues = [_issue("weird", "mystery finding"), _issue("critical", "boom")]
    digest = build_findings_digest(issues, [])
    assert digest.index("[critical]") < digest.index("[weird]")
    assert "mystery finding" in digest


def test_digest_renders_line_and_suggestion() -> None:
    issues = [_issue("high", "desc", file_path="a.py", suggestion="do x", line=42)]
    digest = build_findings_digest(issues, [])
    assert "a.py:42" in digest
    assert "suggestion: do x" in digest


def test_digest_renders_unknown_file_and_omits_empty_suggestion() -> None:
    issues = [_issue("high", "desc", file_path="", suggestion="")]
    digest = build_findings_digest(issues, [])
    assert "(file unknown)" in digest
    assert "suggestion:" not in digest


def test_digest_skips_blank_summaries() -> None:
    digest = build_findings_digest([], ["   ", "real summary"])
    assert "real summary" in digest
    assert "Pass 1" in digest
    assert "Pass 2" not in digest


def test_digest_handles_no_issues_and_no_summaries() -> None:
    digest = build_findings_digest([], [])
    assert "no issues" in digest.lower()
    assert "no per-pass summaries" in digest.lower()
    assert "no per-pass spec-compliance notes" in digest.lower()


def test_digest_includes_per_pass_spec_notes_in_full() -> None:
    """Per-pass spec notes are rendered (the synthesized notes replace the
    concatenated ones, so the evidence must reach the digest)."""
    long_note = "N" * 4_000
    digest = build_findings_digest([], ["summary"], [long_note, "  ", "second note"])
    assert "Per-pass spec-compliance notes" in digest
    assert long_note in digest  # full, untruncated
    assert "second note" in digest
    # Blank notes are skipped, so only two passes are rendered in that section.
    section = digest.split("## Per-pass spec-compliance notes", 1)[1]
    assert "### Pass 1" in section
    assert "### Pass 2" in section
    assert "### Pass 3" not in section


# ---------------------------------------------------------------------------
# synthesize_review_findings — success and None on every failure mode
# ---------------------------------------------------------------------------


class _PayloadClient(DummyLLMClient):
    """Routes the strands Agent call for the synthesis pass to a fixed payload.

    The dummy's strands ``stream`` serializes whatever ``complete_json`` returns,
    so a dict payload becomes the agent's JSON text and a raw string becomes the
    agent's literal (used to drive the malformed/non-object JSON branches).
    """

    def __init__(self, payload: Any) -> None:
        super().__init__()
        self._payload = payload

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        return self._payload


class _RaisingClient(DummyLLMClient):
    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("synthesis model boom")


class _RecordingClient(DummyLLMClient):
    """Captures the synthesis prompt, then returns a fixed payload."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        super().__init__()
        self._payload = payload
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(prompt)
        return self._payload


def test_synthesize_forwards_per_pass_spec_notes_into_prompt() -> None:
    client = _RecordingClient({"summary": "merged", "spec_compliance_notes": "merged notes"})
    result = synthesize_review_findings(
        client,
        input_data=_input(),
        approved=True,
        issues=[],
        chunk_summaries=["s1", "s2"],
        chunk_spec_notes=["AC1 satisfied via endpoint X", "AC2 partially met"],
    )
    assert isinstance(result, SynthesisResult)
    assert len(client.prompts) == 1
    assert "AC1 satisfied via endpoint X" in client.prompts[0]
    assert "AC2 partially met" in client.prompts[0]


def test_synthesize_success_returns_result() -> None:
    client = _PayloadClient({"summary": "merged summary", "spec_compliance_notes": "merged notes"})
    result = synthesize_review_findings(
        client,
        input_data=_input(acceptance_criteria=["AC1", "AC2"]),
        approved=True,
        issues=[_issue("high", "h")],
        chunk_summaries=["s1", "s2"],
    )
    assert isinstance(result, SynthesisResult)
    assert result.summary == "merged summary"
    assert result.spec_compliance_notes == "merged notes"


def test_synthesize_returns_none_on_missing_summary() -> None:
    client = _PayloadClient({"spec_compliance_notes": "notes but no summary"})
    assert (
        synthesize_review_findings(
            client, input_data=_input(), approved=True, issues=[], chunk_summaries=["s1", "s2"]
        )
        is None
    )


def test_synthesize_allows_empty_spec_notes() -> None:
    """A missing/empty ``spec_compliance_notes`` is a valid result — it means the
    reviewers recorded no spec gaps, and the spec-compliance section is omitted."""
    for payload in ({"summary": "only a summary"}, {"summary": "s", "spec_compliance_notes": ""}):
        result = synthesize_review_findings(
            _PayloadClient(payload),
            input_data=_input(),
            approved=True,
            issues=[],
            chunk_summaries=["s1", "s2"],
        )
        assert isinstance(result, SynthesisResult)
        assert result.spec_compliance_notes == ""


def test_synthesize_returns_none_on_empty_values() -> None:
    client = _PayloadClient({"summary": "   ", "spec_compliance_notes": ""})
    assert (
        synthesize_review_findings(
            client, input_data=_input(), approved=False, issues=[], chunk_summaries=["s1", "s2"]
        )
        is None
    )


def test_synthesize_returns_none_on_malformed_json() -> None:
    client = _PayloadClient("<<< not valid json >>>")
    assert (
        synthesize_review_findings(
            client, input_data=_input(), approved=True, issues=[], chunk_summaries=["s1", "s2"]
        )
        is None
    )


def test_synthesize_returns_none_on_non_object_json() -> None:
    client = _PayloadClient('"a bare json string"')
    assert (
        synthesize_review_findings(
            client, input_data=_input(), approved=True, issues=[], chunk_summaries=["s1", "s2"]
        )
        is None
    )


def test_synthesize_returns_none_on_exception() -> None:
    assert (
        synthesize_review_findings(
            _RaisingClient(),
            input_data=_input(),
            approved=False,
            issues=[_issue("critical", "c")],
            chunk_summaries=["s1", "s2"],
        )
        is None
    )


# ---------------------------------------------------------------------------
# synthesize_spec_compliance — single dedicated pass, paired with
# synthesize_review_findings; not yet wired into the coordinator (see its
# own docstring), so these tests exercise it standalone.
# ---------------------------------------------------------------------------


def test_spec_compliance_success_returns_notes_string() -> None:
    client = _PayloadClient({"spec_compliance_notes": "AC2 is not implemented"})
    result = synthesize_spec_compliance(
        client,
        input_data=_input(acceptance_criteria=["AC1", "AC2"]),
        issues=[_issue("high", "missing endpoint")],
    )
    assert result == "AC2 is not implemented"


def test_spec_compliance_allows_empty_notes() -> None:
    """An empty ``spec_compliance_notes`` is a valid, successful result — it
    means no gaps were found, matching CodeReviewOutput's own contract."""
    client = _PayloadClient({"spec_compliance_notes": ""})
    result = synthesize_spec_compliance(client, input_data=_input(), issues=[])
    assert result == ""


def test_spec_compliance_forwards_full_spec_and_criteria_into_prompt() -> None:
    """The full spec/acceptance-criteria text reaches the model verbatim —
    this pass runs once, so it need not compact or truncate that text the way
    a per-chunk prompt would."""
    client = _RecordingClient({"spec_compliance_notes": ""})
    long_spec = "SPEC " * 2_000
    synthesize_spec_compliance(
        client,
        input_data=_input(spec_content=long_spec, acceptance_criteria=["Must support X"]),
        issues=[],
    )
    assert len(client.prompts) == 1
    assert long_spec in client.prompts[0]
    assert "Must support X" in client.prompts[0]


def test_spec_compliance_forwards_merged_issues_into_prompt() -> None:
    client = _RecordingClient({"spec_compliance_notes": ""})
    synthesize_spec_compliance(
        client,
        input_data=_input(),
        issues=[_issue("critical", "SQL injection risk", file_path="app/db.py")],
    )
    assert len(client.prompts) == 1
    assert "SQL injection risk" in client.prompts[0]
    assert "app/db.py" in client.prompts[0]


def test_spec_compliance_returns_none_on_missing_key() -> None:
    client = _PayloadClient({"unrelated_key": "value"})
    assert synthesize_spec_compliance(client, input_data=_input(), issues=[]) is None


def test_spec_compliance_returns_none_on_malformed_json() -> None:
    client = _PayloadClient("<<< not valid json >>>")
    assert synthesize_spec_compliance(client, input_data=_input(), issues=[]) is None


def test_spec_compliance_returns_none_on_non_object_json() -> None:
    client = _PayloadClient('"a bare json string"')
    assert synthesize_spec_compliance(client, input_data=_input(), issues=[]) is None


def test_spec_compliance_returns_none_on_exception() -> None:
    assert (
        synthesize_spec_compliance(
            _RaisingClient(), input_data=_input(), issues=[_issue("critical", "c")]
        )
        is None
    )


def test_spec_compliance_never_mutates_issues() -> None:
    issues = [_issue("high", "h")]
    original = list(issues)
    synthesize_spec_compliance(
        _PayloadClient({"spec_compliance_notes": "gap found"}), input_data=_input(), issues=issues
    )
    assert issues == original


# ---------------------------------------------------------------------------
# coordinator integration — multi-chunk synthesis, single-chunk no-call,
# verdict/issues untouched, concatenation fallback
# ---------------------------------------------------------------------------


def _two_chunk_files() -> Dict[str, str]:
    cap = compute_code_review_map_chunk_chars(DummyLLMClient())
    return {
        "a.py": "a = 1\n".ljust(cap - 1_000, "#"),
        "b.py": "b = 2\n".ljust(cap - 1_000, "#"),
    }


class _NoNotesClient(DummyLLMClient):
    """Per-chunk summaries, but the synthesis pass yields an empty summary → None."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        if "### a.py ###" in prompt:
            return {
                "approved": True,
                "issues": [],
                "summary": "alpha summary",
                "spec_compliance_notes": "",
            }
        if "### b.py ###" in prompt:
            return {
                "approved": True,
                "issues": [],
                "summary": "beta summary",
                "spec_compliance_notes": "",
            }
        # Synthesis pass: empty summary → None → fall back to concatenation.
        return {"summary": "", "spec_compliance_notes": "ignored"}


class _SynthOkClient(DummyLLMClient):
    """One chunk flags a critical issue; the synthesis pass returns clean prose."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        if "### a.py ###" in prompt:
            return {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "logic",
                        "file_path": "a.py",
                        "description": "SQL injection risk",
                        "suggestion": "Use parameterized queries",
                    }
                ],
                "summary": "alpha",
                "spec_compliance_notes": "",
            }
        if "### b.py ###" in prompt:
            return {
                "approved": True,
                "issues": [],
                "summary": "beta",
                "spec_compliance_notes": "",
            }
        return {
            "summary": "SYNTHESIZED SUMMARY",
            "spec_compliance_notes": "SYNTHESIZED NOTES",
        }


def test_coordinator_falls_back_to_concatenation_when_synthesis_unavailable() -> None:
    result = run_coordinator(
        _NoNotesClient(),
        CodeReviewInput(files=_two_chunk_files(), task_description="t", language="python"),
    )
    assert "alpha summary" in result.summary
    assert "beta summary" in result.summary


def test_coordinator_uses_synthesis_without_mutating_verdict_or_issues() -> None:
    result = run_coordinator(
        _SynthOkClient(),
        CodeReviewInput(files=_two_chunk_files(), task_description="t", language="python"),
    )
    # Narrative comes from the synthesis pass...
    assert result.summary == "SYNTHESIZED SUMMARY"
    assert result.spec_compliance_notes == "SYNTHESIZED NOTES"
    # ...but the deterministic verdict and issue list are untouched.
    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "critical"


def test_single_chunk_makes_no_synthesis_call(monkeypatch) -> None:
    calls: list[int] = []
    real = coordinator_mod.synthesize_review_findings

    def _counting(*args: Any, **kwargs: Any):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(coordinator_mod, "synthesize_review_findings", _counting)

    # This same payload serves the (only) chunk-review call here — a single-chunk
    # submission never reaches the synthesis pass — so it must itself be a valid
    # ChunkReviewLLMResponse, not just a bare summary/notes pair.
    client = _PayloadClient(
        {"approved": True, "issues": [], "summary": "x", "spec_compliance_notes": "y"}
    )
    result = run_coordinator(
        client,
        CodeReviewInput(code="### a.py ###\nx = 1", task_description="t", language="python"),
    )
    assert calls == []  # exactly one sub-review → summary/notes pass through directly
    assert result.summary == "x"
    assert result.spec_compliance_notes == "y"


def test_multi_chunk_invokes_synthesis_exactly_once(monkeypatch) -> None:
    calls: list[int] = []
    real = coordinator_mod.synthesize_review_findings

    def _counting(*args: Any, **kwargs: Any):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(coordinator_mod, "synthesize_review_findings", _counting)

    run_coordinator(
        _SynthOkClient(),
        CodeReviewInput(files=_two_chunk_files(), task_description="t", language="python"),
    )
    assert len(calls) == 1
