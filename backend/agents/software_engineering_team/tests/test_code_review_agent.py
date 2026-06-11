"""Tests for CodeReviewAgent (Strands-migrated).

``run`` always delegates to the map-reduce coordinator. Covers small and
large inputs through that path, the ``_reconcile_approval`` safety net
(minor-only override, synthesized issue from summary, zero-feedback
auto-approve), and the new-field propagation for single-chunk reviews.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from code_review_agent import CodeReviewAgent
from code_review_agent.models import CodeReviewInput, CodeReviewOutput

from llm_service.clients.dummy import DummyLLMClient


def _input(code: str = "### app/main.py ###\ndef foo(): pass", **overrides: Any) -> CodeReviewInput:
    base = {
        "code": code,
        "task_description": "Add foo() helper",
        "language": "python",
    }
    base.update(overrides)
    return CodeReviewInput(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Single-call path
# ---------------------------------------------------------------------------


def test_small_code_returns_code_review_output() -> None:
    agent = CodeReviewAgent(llm_client=DummyLLMClient())
    result = agent.run(_input())
    assert isinstance(result, CodeReviewOutput)
    # Dummy stub returns no issues + approved=True via the "senior code reviewer" branch.
    assert result.approved is True
    assert result.issues == []


def test_small_code_with_all_optional_fields_does_not_crash() -> None:
    """spec_content, task_requirements, acceptance_criteria, architecture,
    existing_codebase all plumbed through the builder."""
    from software_engineering_team.shared.models import SystemArchitecture

    arch = SystemArchitecture(
        overview="Tiny service",
        architecture_document="# Arch",
        components=[],
        decisions=[],
        diagrams={},
    )
    agent = CodeReviewAgent(llm_client=DummyLLMClient())
    result = agent.run(
        _input(
            task_requirements="Must support unicode",
            acceptance_criteria=["foo() exists", "foo() is public"],
            spec_content="Project spec: implement foo()",
            architecture=arch,
            existing_codebase="# prior state",
        )
    )
    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True


# ---------------------------------------------------------------------------
# Coordinator (large code) path
# ---------------------------------------------------------------------------


def test_large_code_routes_through_coordinator() -> None:
    """Code larger than one map chunk is reviewed untruncated across multiple
    chunks. End-to-end with DummyLLMClient, the coordinator reviews each chunk
    and merges their output into one CodeReviewOutput."""
    big_file_1 = "### app/main.py ###\n" + ("a" * 25_000)
    big_file_2 = "### app/util.py ###\n" + ("b" * 25_000)
    code = big_file_1 + "\n\n" + big_file_2

    agent = CodeReviewAgent(llm_client=DummyLLMClient())
    result = agent.run(_input(code=code))
    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True
    # Dummy returns "Code review passed (dummy)." per chunk; coordinator
    # concatenates chunk summaries.
    assert "dummy" in result.summary.lower()


# ---------------------------------------------------------------------------
# _reconcile_approval safety net
# ---------------------------------------------------------------------------


class _StubClient(DummyLLMClient):
    """DummyLLMClient subclass that returns a canned response for every complete_json."""

    def __init__(self, canned: Dict[str, Any]) -> None:
        super().__init__()
        self._canned = canned

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return self._canned


def test_reconcile_auto_approves_when_only_minor_issues() -> None:
    """LLM flags approved=False with only low/info issues → override to True."""
    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "low",
                        "category": "naming",
                        "file_path": "app/main.py",
                        "description": "var name could be clearer",
                        "suggestion": "rename x to count",
                    },
                ],
                "summary": "One nit",
            }
        )
    )
    result = agent.run(_input())
    assert result.approved is True
    assert len(result.issues) == 1
    assert result.issues[0].severity == "low"


def test_reconcile_synthesizes_issue_when_rejected_with_summary_and_no_issues() -> None:
    """LLM returns approved=False with 0 issues but a non-empty summary →
    synthesize a high-severity blocking issue from the summary."""
    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": False,
                "issues": [],
                "summary": "Code lacks error handling around DB calls",
            }
        )
    )
    result = agent.run(_input())
    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "high"
    assert "error handling" in result.issues[0].description.lower()


def test_reconcile_auto_approves_when_rejected_with_no_feedback() -> None:
    """LLM returns approved=False with 0 issues AND 0 summary → auto-approve
    (prevents unresolvable loops)."""
    agent = CodeReviewAgent(
        llm_client=_StubClient({"approved": False, "issues": [], "summary": ""})
    )
    result = agent.run(_input())
    assert result.approved is True
    assert result.issues == []


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``CodeReviewAgent`` instance must handle many
    ``run()`` calls in sequence. Early Strands migrations cached a Strands
    ``Agent`` instance in ``__init__`` and reused it across calls, which
    broke the ``structured_output_model`` forced-tool-choice on the second
    call because Strands' Agent accumulates message history. The fix is to
    construct a fresh Strands Agent per ``run()``.
    """
    agent = CodeReviewAgent(llm_client=DummyLLMClient())
    for i in range(4):
        result = agent.run(_input(code=f"### app/m{i}.py ###\ndef f{i}(): pass"))
        assert isinstance(result, CodeReviewOutput)
        assert result.approved is True, f"run {i} failed: {result.summary}"


def test_small_code_routes_through_coordinator_chunk_path() -> None:
    """``run`` always delegates to the coordinator: even tiny inputs are
    reviewed through the chunk-review prompt, not a separate single-call path."""

    class _Recorder(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.prompts: list[str] = []

        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            self.prompts.append(prompt)
            return super().complete_json(prompt, **kwargs)

    client = _Recorder()
    agent = CodeReviewAgent(llm_client=client)
    result = agent.run(_input())
    assert result.approved is True
    assert len(client.prompts) == 1
    # CHUNK_REVIEW_NOTE marker proves the coordinator's map path was used.
    assert "one chunk of the full codebase" in client.prompts[0]


def test_single_chunk_propagates_notes_and_commit_message_through_agent() -> None:
    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": True,
                "issues": [],
                "summary": "Looks good.",
                "spec_compliance_notes": "Meets the acceptance criteria.",
                "suggested_commit_message": "feat: add foo helper",
            }
        )
    )
    result = agent.run(_input())
    assert result.approved is True
    assert result.spec_compliance_notes == "Meets the acceptance criteria."
    assert result.suggested_commit_message == "feat: add foo helper"


def test_agent_accepts_files_dict_input() -> None:
    agent = CodeReviewAgent(llm_client=DummyLLMClient())
    result = agent.run(
        CodeReviewInput(
            files={"app/main.py": "def foo(): pass"},
            task_description="Add foo() helper",
            language="python",
        )
    )
    assert isinstance(result, CodeReviewOutput)
    assert result.approved is True


def test_run_raises_unavailable_when_review_cannot_complete() -> None:
    """Contract: a review that cannot be completed raises — callers must treat
    it as a failed run, never as feedback for the coding agent."""
    import pytest
    from code_review_agent.models import CodeReviewUnavailableError

    from llm_service import LLMRateLimitError

    class _AlwaysRateLimited(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise LLMRateLimitError("429")

    agent = CodeReviewAgent(llm_client=_AlwaysRateLimited())
    with pytest.raises(CodeReviewUnavailableError):
        agent.run(_input())


def test_reconcile_rejects_when_critical_issue_present() -> None:
    """LLM returns a critical issue with approved=True → override to False."""
    agent = CodeReviewAgent(
        llm_client=_StubClient(
            {
                "approved": True,  # deliberately wrong
                "issues": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "file_path": "app/main.py",
                        "description": "SQL injection in user query",
                        "suggestion": "Use parameterized queries",
                    },
                ],
                "summary": "LGTM",
            }
        )
    )
    result = agent.run(_input())
    assert result.approved is False
    assert len(result.issues) == 1
    assert result.issues[0].severity == "critical"
