"""Tests for QAExpertAgent and its Pydantic models.

Covers the model-level cleanup logic that moved out of the agent when it
migrated to the Strands adapter (``BugReport`` location collapse and
``QAOutput`` newline unescaping), plus an end-to-end run against
``DummyLLMClient`` in each of the three request modes.
"""

from __future__ import annotations

from qa_agent import QAExpertAgent, QAInput
from qa_agent.models import BugReport, QAOutput

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture

# ---------------------------------------------------------------------------
# BugReport.location collapse validator
# ---------------------------------------------------------------------------


def test_bug_report_collapses_file_path_and_line_into_location() -> None:
    bug = BugReport(
        severity="high",
        description="missing import",
        file_path="app/main.py",
        line_or_section="42",
    )
    assert bug.location == "app/main.py:42"


def test_bug_report_collapses_file_path_only_when_line_missing() -> None:
    bug = BugReport(
        severity="medium",
        description="bad return type",
        file_path="app/utils.py",
    )
    assert bug.location == "app/utils.py"


def test_bug_report_prefers_explicit_location_over_file_path() -> None:
    bug = BugReport(
        severity="low",
        description="typo",
        location="already/set.py:9",
        file_path="never/used.py",
        line_or_section="99",
    )
    assert bug.location == "already/set.py:9"


def test_bug_report_location_stays_empty_when_nothing_provided() -> None:
    bug = BugReport(severity="info", description="general note")
    assert bug.location == ""


# ---------------------------------------------------------------------------
# QAOutput.\n unescaping validator
# ---------------------------------------------------------------------------


def test_qa_output_unescapes_literal_newlines_in_code_fields() -> None:
    out = QAOutput(
        integration_tests="def test_a():\\n    assert True",
        unit_tests="def test_b():\\n    assert 1 == 1",
        readme_content="# Title\\n\\n## Section",
    )
    assert out.integration_tests == "def test_a():\n    assert True"
    assert out.unit_tests == "def test_b():\n    assert 1 == 1"
    assert out.readme_content == "# Title\n\n## Section"


def test_qa_output_leaves_real_newlines_alone() -> None:
    out = QAOutput(
        integration_tests="line1\nline2",
        unit_tests="",
        readme_content="already\ngood",
    )
    assert out.integration_tests == "line1\nline2"
    assert out.readme_content == "already\ngood"


def test_qa_output_non_code_fields_not_touched_by_validator() -> None:
    # ``summary`` and ``live_test_notes`` are natural language; they should
    # pass through untouched even if they happen to contain the ``\n`` token.
    out = QAOutput(summary="literal \\n in summary is fine")
    assert out.summary == "literal \\n in summary is fine"


# ---------------------------------------------------------------------------
# End-to-end: QAExpertAgent.run with DummyLLMClient
# ---------------------------------------------------------------------------


def _input(**overrides: object) -> QAInput:
    base = {
        "code": "def add(a, b):\n    return a + b",
        "language": "python",
        "task_description": "Implement a simple add function",
    }
    base.update(overrides)
    return QAInput(**base)  # type: ignore[arg-type]


def test_qa_expert_agent_default_mode_returns_qa_output() -> None:
    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(_input())
    assert isinstance(result, QAOutput)
    assert result.approved is True
    assert result.bugs_found == []
    assert result.summary  # dummy stub sets a non-empty summary
    assert "dummy" in result.integration_tests.lower()


def test_qa_expert_agent_write_tests_mode() -> None:
    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(_input(request_mode="write_tests"))
    assert isinstance(result, QAOutput)
    # Dummy stub returns both unit_tests and integration_tests in write_tests mode.
    assert result.unit_tests
    assert result.integration_tests


def test_qa_expert_agent_fix_build_mode_with_build_errors() -> None:
    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(
        _input(
            request_mode="fix_build",
            build_errors="SyntaxError: invalid syntax on line 3",
        )
    )
    assert isinstance(result, QAOutput)
    # Dummy stub has no bugs, so approved stays True. The point of this test
    # is that the fix_build code path doesn't raise and still returns a
    # well-formed QAOutput.
    assert result.approved is True


def test_qa_expert_agent_derives_approved_from_bug_severities() -> None:
    """If the LLM returns critical bugs, ``approved`` should be False even
    when the LLM set it to True."""

    class _LyingClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "bugs_found": [
                    {"severity": "critical", "description": "NPE in /auth"},
                    {"severity": "low", "description": "typo"},
                ],
                "approved": True,  # deliberately wrong
                "summary": "LGTM",
                "integration_tests": "",
                "unit_tests": "",
                "test_plan": "",
                "live_test_notes": "",
                "readme_content": "",
                "suggested_commit_message": "",
            }

    agent = QAExpertAgent(_LyingClient())
    result = agent.run(_input())
    assert result.approved is False
    assert len(result.bugs_found) == 2
    assert result.bugs_found[0].severity == "critical"


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``QAExpertAgent`` instance must handle many
    ``run()`` calls in sequence across different request modes. See
    test_code_review_agent.py::test_multiple_run_calls_on_same_instance_succeed
    for the root-cause details."""
    agent = QAExpertAgent(DummyLLMClient())
    modes: list[str | None] = [None, "write_tests", None, "write_tests"]
    for i, mode in enumerate(modes):
        result = agent.run(_input(request_mode=mode))
        assert isinstance(result, QAOutput), f"run {i} (mode={mode}) did not return QAOutput"
        assert result.approved is True, f"run {i} (mode={mode}) failed: {result.summary}"


# ---------------------------------------------------------------------------
# _build_user_prompt: file context moved to system content (Story 2c Step 2)
# ---------------------------------------------------------------------------


def test_user_prompt_contains_file_context_and_role_instructions() -> None:
    """The user prompt carries both the file-context prefix (language + code
    under review) and the role instructions. Untrusted code must stay in the
    user message, not be elevated to system-level instructions."""
    prompt = QAExpertAgent._build_user_prompt(_input())
    # Role instructions are present
    assert "Review the code for bugs" in prompt
    assert "**Task:**" in prompt
    # File-context prefix IS in the user prompt (untrusted code stays here)
    assert "def add(a, b)" in prompt
    assert "**Language:**" in prompt


def test_user_prompt_includes_architecture_run_instructions_and_build_errors() -> None:
    """Optional role-instruction sections are still in the user prompt."""
    prompt = QAExpertAgent._build_user_prompt(
        _input(
            architecture=SystemArchitecture(overview="layered"),
            run_instructions="uvicorn main:app",
            build_errors="SyntaxError: bad",
        )
    )
    assert "**Architecture:**" in prompt
    assert "**Run instructions:**" in prompt
    assert "**Build/compiler errors:**" in prompt
    # File-context prefix IS in the user prompt
    assert "def add(a, b)" in prompt


def test_qa_expert_agent_falls_back_on_validation_error() -> None:
    """A malformed LLM response must not crash the pipeline."""

    class _BrokenClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {"not_a_qa_output_shape": True}

    agent = QAExpertAgent(_BrokenClient())
    result = agent.run(_input())
    # QAOutput accepts missing fields (they all have defaults), so the
    # fallback isn't actually triggered here — assert the graceful path
    # instead: a well-formed empty QAOutput with approved=True (no bugs).
    assert isinstance(result, QAOutput)
    assert result.bugs_found == []


# ---------------------------------------------------------------------------
# acceptance_evidence mode (absorbs the former DevOps test-validation surface)
# ---------------------------------------------------------------------------


def test_acceptance_evidence_system_prompt_is_standalone() -> None:
    """The acceptance_evidence persona must not embed the bug-review prompt,
    whose 'review code for bugs' directions contradict release validation."""
    from qa_agent.prompts import QA_PROMPT, QA_PROMPT_ACCEPTANCE_EVIDENCE

    agent = QAExpertAgent(DummyLLMClient())
    sp = agent._system_prompts["acceptance_evidence"]
    # Structural assertions (independent of the exact wording of either prompt):
    # the acceptance_evidence system prompt is exactly the standalone persona and
    # does not embed the bug-review QA_PROMPT.
    assert sp == QA_PROMPT_ACCEPTANCE_EVIDENCE
    assert QA_PROMPT not in sp
    # And it carries acceptance-evidence-specific instructions.
    assert "acceptance criteria" in sp.lower()


def test_qa_expert_agent_acceptance_evidence_mode_maps_evidence() -> None:
    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(
        _input(
            request_mode="acceptance_evidence",
            acceptance_criteria=["Criterion 1"],
            tool_results={"unit": {"unit_tests": "pass"}},
        )
    )
    assert isinstance(result, QAOutput)
    assert result.approved is True
    assert result.quality_gates  # populated by the acceptance-evidence dummy anchor
    assert result.acceptance_trace
    assert result.validation_evidence
    # bug-review fields stay empty in this mode.
    assert result.bugs_found == []


def test_qa_expert_agent_acceptance_evidence_gate_fail_forces_unapproved() -> None:
    """A failing quality gate blocks approval even when the LLM says approved."""

    class _GateFailClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "approved": True,  # deliberately optimistic
                "quality_gates": {"unit_tests": "pass", "deploy_dry_run": "fail"},
                "acceptance_trace": [],
                "validation_evidence": [],
                "bugs_found": [],
                "summary": "one gate failed",
            }

    agent = QAExpertAgent(_GateFailClient())
    result = agent.run(_input(request_mode="acceptance_evidence", acceptance_criteria=["c1"]))
    assert result.approved is False


def test_qa_expert_agent_acceptance_evidence_whitespace_fail_forces_unapproved() -> None:
    """A whitespace-padded ``" fail "`` gate must still block approval."""

    class _PaddedFailClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "approved": True,
                "quality_gates": {"deploy_dry_run": "  FAIL  "},
                "acceptance_trace": [],
                "validation_evidence": [],
                "bugs_found": [],
                "summary": "padded fail",
            }

    agent = QAExpertAgent(_PaddedFailClient())
    result = agent.run(_input(request_mode="acceptance_evidence", acceptance_criteria=["c1"]))
    assert result.approved is False


def test_qa_expert_agent_acceptance_evidence_all_pass_approves() -> None:
    class _AllPassClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "approved": True,
                "quality_gates": {"unit_tests": "pass", "integration_tests": "skipped"},
                "acceptance_trace": [{"criterion": "c1", "implementation_refs": [], "tests": []}],
                "validation_evidence": [{"gate": "unit_tests", "status": "pass", "detail": "ok"}],
                "bugs_found": [],
                "summary": "all good",
            }

    agent = QAExpertAgent(_AllPassClient())
    result = agent.run(_input(request_mode="acceptance_evidence", acceptance_criteria=["c1"]))
    assert result.approved is True
    assert result.validation_evidence[0]["gate"] == "unit_tests"


def test_qa_expert_agent_acceptance_evidence_unapproved_without_fail_gate_synthesizes_one() -> None:
    """An unapproved verdict with no failing gate must synthesize one so a
    gate-only downstream consumer (e.g. the DevOps quality gate) still blocks."""

    class _UnapprovedNoFailGateClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "approved": False,  # unapproved but no "fail" gate present
                "quality_gates": {"unit_tests": "not_run"},
                "acceptance_trace": [],
                "validation_evidence": [],
                "bugs_found": [],
                "summary": "could not validate",
            }

    agent = QAExpertAgent(_UnapprovedNoFailGateClient())
    result = agent.run(_input(request_mode="acceptance_evidence", acceptance_criteria=["c1"]))
    assert result.approved is False
    assert any(v == "fail" for v in result.quality_gates.values())


def test_qa_expert_agent_acceptance_evidence_approved_does_not_synthesize_fail_gate() -> None:
    class _AllPassClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "approved": True,
                "quality_gates": {"unit_tests": "pass"},
                "acceptance_trace": [],
                "validation_evidence": [],
                "bugs_found": [],
                "summary": "ok",
            }

    agent = QAExpertAgent(_AllPassClient())
    result = agent.run(_input(request_mode="acceptance_evidence", acceptance_criteria=["c1"]))
    assert result.approved is True
    assert "acceptance_evidence" not in result.quality_gates


def test_qa_expert_agent_acceptance_evidence_fallback_fails_closed(monkeypatch) -> None:
    """A structured-output failure in acceptance_evidence mode must fail
    closed with a synthesized failing gate, not an empty gate map."""
    import qa_agent.agent as agent_mod

    class _RaisingAgent:
        def __call__(self, *a: object, **kw: object) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_mod, "Agent", lambda *, model, system_prompt: _RaisingAgent())

    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(_input(request_mode="acceptance_evidence", acceptance_criteria=["c1"]))
    assert result.approved is False
    assert result.quality_gates.get("acceptance_evidence") == "fail"
