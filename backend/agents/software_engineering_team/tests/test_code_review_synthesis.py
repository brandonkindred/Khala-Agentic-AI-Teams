"""Tests for the reduce-phase findings synthesis (Stage 2).

``synthesis.py`` owns the reduce-phase narrative synthesis: a findings-only LLM
pass that merges per-chunk summaries/notes (``synthesize_review_findings``),
and a separate spec-compliance LLM pass that checks the merged findings
against the full spec/acceptance-criteria text once
(``synthesize_spec_compliance``). These tests cover the digest builder
(ordering and completeness, no code), both synthesis calls' success paths and
their ``None``/fallback behavior on every failure mode, and the coordinator
integration — ``synthesize_review_findings`` is used for multi-chunk reviews,
skipped for single-chunk ones, never mutates the verdict or issues, and falls
back to concatenation when it returns ``None``.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict

import pytest
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
    base: Dict[str, Any] = dict(files={"a.py": "x = 1"}, task_description="t", language="python")
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
    """Routes think-then-format synthesis to a fixed payload on the format pass.

    Call 1 (Agent ``stream``) and call 2 (``complete_json``) both delegate here;
    a dict payload becomes JSON text on call 1 and the parsed object on call 2.
    A raw string payload drives malformed/non-object JSON branches on call 2.
    """

    def __init__(self, payload: Any) -> None:
        super().__init__()
        self._payload = payload

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        return self._payload


class _SynthesisTwoCallStub(DummyLLMClient):
    """Records both synthesis passes: prose on call 1 (via ``stream``), JSON on call 2."""

    def __init__(
        self, canned: Dict[str, Any], *, prose: str = "Structured prose synthesis."
    ) -> None:
        super().__init__()
        self._canned = canned
        self._prose = prose
        self.complete_json_calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        objective: str = "dummy",
        think: bool | str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.complete_json_calls.append(
            {"prompt": prompt, "objective": objective, "think": think, **kwargs}
        )
        if len(self.complete_json_calls) == 1:
            return self._prose
        return self._canned


class _SynthesisCall1FailClient(DummyLLMClient):
    """Raises on the first ``complete_json`` (reasoning pass via ``stream``)."""

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        raise RuntimeError("synthesis reasoning boom")


class _SynthesisCall2FailClient(DummyLLMClient):
    """Returns prose on call 1, then raises on the formatting ``complete_json``."""

    _calls = 0

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        _SynthesisCall2FailClient._calls += 1
        if _SynthesisCall2FailClient._calls == 1:
            return "Structured prose synthesis."
        raise RuntimeError("synthesis format boom")


class _RaisingClient(DummyLLMClient):
    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("synthesis model boom")


class _RecordingClient(DummyLLMClient):
    """Captures reasoning vs formatting prompts across the two-call split."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        super().__init__()
        self._payload = payload
        self.reasoning_prompts: list[str] = []
        self.format_prompts: list[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        if _is_format_pass_prompt(prompt):
            self.format_prompts.append(prompt)
            return self._payload
        self.reasoning_prompts.append(prompt)
        return "Structured prose synthesis."


def _is_format_pass_prompt(prompt: str) -> bool:
    return "Convert the following analysis" in prompt or "--- ANALYSIS" in prompt


def _last_reasoning_prompt() -> str:
    return getattr(_REASONING_PROMPT_LOCAL, "value", "")


def _set_last_reasoning_prompt(prompt: str) -> None:
    _REASONING_PROMPT_LOCAL.value = prompt


_REASONING_PROMPT_LOCAL = threading.local()


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
    assert len(client.reasoning_prompts) == 1
    assert "AC1 satisfied via endpoint X" in client.reasoning_prompts[0]
    assert "AC2 partially met" in client.reasoning_prompts[0]


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


def test_synthesize_records_reasoning_conversation_in_transcript(monkeypatch) -> None:
    """The durable transcript captures the reasoning-pass conversation, not just
    the formatting JSON, so the synthesizer's thinking is inspectable."""
    from llm_service import llm_attribution

    captured: list = []
    monkeypatch.setattr(
        "code_review_agent.synthesis.record_transcript_entry",
        lambda *args, **kwargs: captured.append(args),
    )
    client = _PayloadClient({"summary": "merged summary", "spec_compliance_notes": ""})
    with llm_attribution(job_id="job-1"):
        result = synthesize_review_findings(
            client,
            input_data=_input(),
            approved=True,
            issues=[_issue("high", "h")],
            chunk_summaries=["s1"],
        )
    assert isinstance(result, SynthesisResult)
    assert len(captured) == 1
    stage, _target, prompt, response = captured[0]
    assert stage == "synthesis"
    assert "deterministic review verdict" in prompt.lower()
    messages = json.loads(response)
    assert isinstance(messages, list)
    assert len(messages) >= 2
    assert messages[0]["role"] == "user"


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


def test_synthesize_review_findings_two_call_split_prose_then_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review synthesis uses ``run_agent_via_reasoning``: prose Agent call 1, JSON call 2."""
    import code_review_agent.via_reasoning as vr_mod

    agent_calls: list[dict[str, Any]] = []
    real_agent_cls = vr_mod.Agent

    class _RecordingAgent(real_agent_cls):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            agent_calls.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    client = _SynthesisTwoCallStub(
        {"summary": "merged summary", "spec_compliance_notes": "merged notes"}
    )
    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    result = synthesize_review_findings(
        client,
        input_data=_input(),
        approved=True,
        issues=[_issue("high", "h")],
        chunk_summaries=["s1", "s2"],
    )

    assert isinstance(result, SynthesisResult)
    assert result.summary == "merged summary"
    assert len(agent_calls) == 1
    assert agent_calls[0]["tools"] == []
    assert "Return a single JSON object" not in (agent_calls[0].get("system_prompt") or "")
    assert len(client.complete_json_calls) == 2
    assert client.complete_json_calls[1]["think"] is False
    assert "summary" in client.complete_json_calls[1]["prompt"].lower()


def test_synthesize_review_findings_returns_none_when_reasoning_call_fails() -> None:
    assert (
        synthesize_review_findings(
            _SynthesisCall1FailClient(),
            input_data=_input(),
            approved=True,
            issues=[],
            chunk_summaries=["s1", "s2"],
        )
        is None
    )


def test_synthesize_review_findings_returns_none_when_format_call_fails() -> None:
    _SynthesisCall2FailClient._calls = 0
    assert (
        synthesize_review_findings(
            _SynthesisCall2FailClient(),
            input_data=_input(),
            approved=True,
            issues=[],
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
    assert len(client.reasoning_prompts) == 1
    assert long_spec in client.reasoning_prompts[0]
    assert "Must support X" in client.reasoning_prompts[0]


def test_spec_compliance_forwards_merged_issues_into_prompt() -> None:
    client = _RecordingClient({"spec_compliance_notes": ""})
    synthesize_spec_compliance(
        client,
        input_data=_input(),
        issues=[_issue("critical", "SQL injection risk", file_path="app/db.py")],
    )
    assert len(client.reasoning_prompts) == 1
    assert "SQL injection risk" in client.reasoning_prompts[0]
    assert "app/db.py" in client.reasoning_prompts[0]


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


def test_synthesize_spec_compliance_two_call_split_prose_then_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec-compliance synthesis uses ``run_agent_via_reasoning`` with no tools."""
    import code_review_agent.via_reasoning as vr_mod

    agent_calls: list[dict[str, Any]] = []
    real_agent_cls = vr_mod.Agent

    class _RecordingAgent(real_agent_cls):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            agent_calls.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    client = _SynthesisTwoCallStub({"spec_compliance_notes": "AC2 gap"})
    monkeypatch.setattr(vr_mod, "Agent", _RecordingAgent)

    result = synthesize_spec_compliance(
        client,
        input_data=_input(acceptance_criteria=["AC1", "AC2"]),
        issues=[_issue("high", "missing endpoint")],
    )

    assert result == "AC2 gap"
    assert len(agent_calls) == 1
    assert agent_calls[0]["tools"] == []
    assert "Return a single JSON object" not in (agent_calls[0].get("system_prompt") or "")
    assert len(client.complete_json_calls) == 2
    assert client.complete_json_calls[1]["think"] is False
    assert "spec_compliance_notes" in client.complete_json_calls[1]["prompt"].lower()


def test_synthesize_spec_compliance_returns_none_when_reasoning_call_fails() -> None:
    assert (
        synthesize_spec_compliance(
            _SynthesisCall1FailClient(),
            input_data=_input(),
            issues=[_issue("critical", "c")],
        )
        is None
    )


def test_synthesize_spec_compliance_returns_none_when_format_call_fails() -> None:
    _SynthesisCall2FailClient._calls = 0
    assert (
        synthesize_spec_compliance(
            _SynthesisCall2FailClient(),
            input_data=_input(),
            issues=[_issue("critical", "c")],
        )
        is None
    )


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
    """Per-chunk summaries; synthesis format pass yields an empty summary → None."""

    def complete(self, prompt: str, **kwargs: Any) -> str:
        _set_last_reasoning_prompt(prompt)
        return "Structured prose chunk review."

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        if _is_format_pass_prompt(prompt):
            lower = prompt.lower()
            if (
                '"summary"' in lower
                and '"spec_compliance_notes"' in lower
                and '"approved"' not in lower
            ):
                return {"summary": "", "spec_compliance_notes": "ignored"}
            last = _last_reasoning_prompt()
            if "### a.py ###" in last:
                return {
                    "approved": True,
                    "issues": [],
                    "summary": "alpha summary",
                    "spec_compliance_notes": "",
                }
            if "### b.py ###" in last:
                return {
                    "approved": True,
                    "issues": [],
                    "summary": "beta summary",
                    "spec_compliance_notes": "",
                }
        return "Structured prose synthesis."


class _SynthOkClient(DummyLLMClient):
    """One chunk flags a critical issue; synthesis format pass returns clean prose."""

    def complete(self, prompt: str, **kwargs: Any) -> str:
        _set_last_reasoning_prompt(prompt)
        return "Structured prose chunk review."

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        if _is_format_pass_prompt(prompt):
            lower = prompt.lower()
            if (
                '"summary"' in lower
                and '"spec_compliance_notes"' in lower
                and '"approved"' not in lower
            ):
                return {
                    "summary": "SYNTHESIZED SUMMARY",
                    "spec_compliance_notes": "SYNTHESIZED NOTES",
                }
            last = _last_reasoning_prompt()
            if "### a.py ###" in last:
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
            if "### b.py ###" in last:
                return {
                    "approved": True,
                    "issues": [],
                    "summary": "beta",
                    "spec_compliance_notes": "",
                }
        return "Structured prose synthesis."


def _coordinator_input(**kwargs: Any) -> CodeReviewInput:
    base: Dict[str, Any] = dict(
        files=_two_chunk_files(), task_description="t", language="python", skip_tail_passes=True
    )
    base.update(kwargs)
    return CodeReviewInput(**base)


def test_coordinator_falls_back_to_concatenation_when_synthesis_unavailable() -> None:
    """When the narrative synthesis pass returns an empty summary, the
    coordinator must fall back to concatenating per-chunk summaries and
    spec_compliance_notes without changing the deterministic verdict or issue
    list."""
    result = run_coordinator(
        _NoNotesClient(),
        _coordinator_input(),
    )
    assert "alpha summary" in result.summary
    assert "beta summary" in result.summary
    # Per-chunk spec_compliance_notes are both "" here, so the concatenation
    # fallback must also be "" -- not the synthesis pass's (unused) "ignored".
    assert result.spec_compliance_notes == ""


def test_coordinator_uses_synthesis_without_mutating_verdict_or_issues() -> None:
    """When the synthesis pass succeeds, its summary/notes replace the
    per-chunk narrative, but the deterministic verdict and issue list --
    already decided before synthesis runs -- are never touched."""
    result = run_coordinator(
        _SynthOkClient(),
        _coordinator_input(),
    )
    # Narrative comes from the synthesis pass...
    assert result.summary == "SYNTHESIZED SUMMARY"
    assert result.spec_compliance_notes == "SYNTHESIZED NOTES"
    # ...but the deterministic verdict and issue list are untouched.
    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "critical"


def test_single_chunk_makes_no_synthesis_call(monkeypatch) -> None:
    """A submission that reviews as exactly one chunk has nothing to merge, so
    the coordinator must skip the synthesis LLM call entirely and pass that
    chunk's own summary/notes through verbatim."""
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
        CodeReviewInput(files={"a.py": "x = 1"}, task_description="t", language="python"),
    )
    assert calls == []  # exactly one sub-review → summary/notes pass through directly
    assert result.summary == "x"
    assert result.spec_compliance_notes == "y"


def test_multi_chunk_invokes_synthesis_exactly_once(monkeypatch) -> None:
    """A submission that reviews as multiple chunks must invoke the synthesis
    pass exactly once per run, not once per chunk."""
    calls: list[int] = []
    real = coordinator_mod.synthesize_review_findings

    def _counting(*args: Any, **kwargs: Any):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(coordinator_mod, "synthesize_review_findings", _counting)

    run_coordinator(
        _SynthOkClient(),
        _coordinator_input(),
    )
    assert len(calls) == 1
