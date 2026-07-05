"""Tests for Code Review Chunk Reviewer (Strands-migrated).

Uses ``DummyLLMClient`` subclasses instead of ``MagicMock``: the Strands
adapter path doesn't call ``llm.complete_json`` directly (it goes through
``chat_json_round`` with a ``StructuredOutputTool``), so mock-based
assertions on ``complete_json.call_args`` are no longer meaningful.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from code_review_agent.chunk_reviewer import ChunkReviewAgent, review_chunk
from code_review_agent.models import ChunkReviewInput, ChunkReviewOutput

from llm_service.clients.dummy import DummyLLMClient


class _StubClient(DummyLLMClient):
    """DummyLLMClient subclass returning a canned CodeReview-shaped dict."""

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


def _chunk_input(**overrides: Any) -> ChunkReviewInput:
    base = {
        "code_chunk": "### app/main.py ###\ndef foo(): pass",
        "file_path_or_label": "app/main.py",
        "task_description": "Add endpoint",
        "task_requirements": "",
        "acceptance_criteria": [],
        "spec_excerpt": "",
        "architecture_overview": "",
        "existing_codebase_excerpt": None,
    }
    base.update(overrides)
    return ChunkReviewInput(**base)  # type: ignore[arg-type]


def test_review_chunk_legacy_wrapper_returns_dict_with_expected_keys() -> None:
    """Legacy ``review_chunk`` helper delegates to ChunkReviewAgent but
    still returns a plain dict for backward compat."""
    result = review_chunk(
        llm=DummyLLMClient(),
        code_chunk="### app/main.py ###\ndef foo(): pass",
        file_paths_label="app/main.py",
        task_description="Add endpoint",
        task_requirements="",
        acceptance_criteria=[],
        spec_excerpt="",
        architecture_overview="",
        existing_codebase_excerpt=None,
    )
    assert isinstance(result, dict)
    # Dummy stub returns approved=True with no issues.
    assert result["approved"] is True
    assert result["issues"] == []
    assert "summary" in result


def test_chunk_review_agent_run_returns_chunk_review_output() -> None:
    agent = ChunkReviewAgent(llm=DummyLLMClient())
    result = agent.run(_chunk_input())
    assert isinstance(result, ChunkReviewOutput)
    assert result.approved is True
    assert result.issues == []


def test_chunk_review_agent_carries_file_path_from_issue() -> None:
    """When the LLM sets a file_path on an issue, it flows through to the
    output unchanged."""
    agent = ChunkReviewAgent(
        llm=_StubClient(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "file_path": "app/models.py",
                        "description": "Missing docstring on User",
                        "suggestion": "Add a docstring describing fields",
                    },
                ],
                "summary": "One issue found.",
            }
        )
    )
    result = agent.run(_chunk_input(file_path_or_label="app/models.py"))
    assert isinstance(result, ChunkReviewOutput)
    assert result.approved is False
    assert len(result.issues) == 1
    # ``ChunkReviewOutput.issues`` remains ``List[Dict[str, Any]]`` for
    # backward compat — callers can still index by key.
    assert result.issues[0]["file_path"] == "app/models.py"
    assert result.issues[0]["description"] == "Missing docstring on User"
    assert "One issue" in result.summary


class _RecorderClient(DummyLLMClient):
    """Delegates to Dummy but records every prompt."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(prompt)
        return super().complete_json(prompt, **kwargs)


def test_segment_note_is_prepended_to_prompt() -> None:
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    note = "app/main.py is shown only from original line 501 to 1000 (of 2400 total)."
    agent.run(_chunk_input(segment_note=note))
    assert len(client.prompts) == 1
    prompt = client.prompts[0]
    assert "**Segment notes:**" in prompt
    assert note in prompt
    assert prompt.index(note) < prompt.index("Code to review")


def test_no_segment_note_means_no_segment_section() -> None:
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input())
    assert "**Segment notes:**" not in client.prompts[0]


def test_review_guardrails_note_is_in_every_prompt() -> None:
    """The anti-false-positive guardrails (no phantom truncation, don't flag
    existing files as missing, relative imports are conventional) are injected
    into the per-chunk user prompt (not the byte-locked system prompt)."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input())
    prompt = client.prompts[0]
    assert "**Review guardrails" in prompt
    assert "COMPLETE" in prompt  # units shown are complete -> no phantom truncation
    assert "does not exist" in prompt  # don't flag existing files as missing
    assert "from .models import" in prompt  # relative imports are conventional


def test_user_decisions_rendered_as_settled_in_prompt() -> None:
    """A resolved user decision is surfaced to the reviewer as settled so it is never flagged
    as an open/unanswered question."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(user_decisions=["Which auth? → OAuth2 (Google)"]))
    prompt = client.prompts[0]
    assert "User decisions already made" in prompt
    assert "Which auth? → OAuth2 (Google)" in prompt


def test_no_user_decisions_means_no_decisions_section() -> None:
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input())
    assert "User decisions already made" not in client.prompts[0]


def test_code_chunk_is_never_compacted() -> None:
    """The coordinator bounds the chunk; the reviewer must send it verbatim.
    A chunk above the map budget is logged but not truncated, so a sentinel at
    the very end must survive into the prompt."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_map_chunk_chars,
    )

    client = _RecorderClient()
    budget = compute_code_review_map_chunk_chars(client)
    sentinel = "UNIQUE_TAIL_SENTINEL_42"
    chunk = ("x = 1\n" * ((budget // 6) + 200)) + sentinel
    assert len(chunk) > budget

    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(code_chunk=chunk))
    assert sentinel in client.prompts[0]


def test_new_output_fields_are_parsed_through() -> None:
    agent = ChunkReviewAgent(
        llm=_StubClient(
            {
                "approved": True,
                "issues": [],
                "summary": "ok",
                "spec_compliance_notes": "Chunk meets the spec.",
                "suggested_commit_message": "refactor: tidy main",
            }
        )
    )
    result = agent.run(_chunk_input())
    assert result.spec_compliance_notes == "Chunk meets the spec."
    assert result.suggested_commit_message == "refactor: tidy main"


def test_missing_new_output_fields_default_to_empty() -> None:
    agent = ChunkReviewAgent(llm=_StubClient({"approved": True, "issues": [], "summary": "ok"}))
    result = agent.run(_chunk_input())
    assert result.spec_compliance_notes == ""
    assert result.suggested_commit_message == ""


def test_chunk_review_agent_passes_blank_file_path_through_unchanged() -> None:
    """A blank/missing issue file_path is passed through raw — never filled
    with the chunk label, which would defeat the coordinator's per-path offset
    lookup (the coordinator resolves blank paths itself)."""
    agent = ChunkReviewAgent(
        llm=_StubClient(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "naming",
                        "description": "Use snake_case",
                        "suggestion": "Rename to get_user",
                    },
                ],
                "summary": "Fix naming.",
            }
        )
    )
    result = agent.run(_chunk_input(file_path_or_label="app/main.py"))
    assert len(result.issues) == 1
    assert "file_path" not in result.issues[0]
    assert result.issues[0]["severity"] == "high"


def test_declared_language_reaches_prompt_without_heuristic() -> None:
    """The caller's language is used verbatim; the def-sniffing heuristic only
    applies when no language was declared."""
    client = _RecorderClient()
    agent = ChunkReviewAgent(llm=client)
    # No "def " in the chunk: the heuristic alone would say typescript.
    agent.run(_chunk_input(code_chunk="TIMEOUT = 30", language="python"))
    assert "**Language:** python" in client.prompts[0]

    fallback_client = _RecorderClient()
    ChunkReviewAgent(llm=fallback_client).run(_chunk_input(code_chunk="TIMEOUT = 30"))
    assert "**Language:** typescript" in fallback_client.prompts[0]


def test_shared_context_is_hard_capped_deterministically() -> None:
    """Spec/arch/existing excerpts are sliced to budget here — no LLM
    compaction calls — so an upstream compaction failure can never balloon the
    chunk prompt."""
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_spec_excerpt_chars,
    )

    client = _RecorderClient()
    max_spec = compute_code_review_spec_excerpt_chars(client)
    oversized_spec = ("S" * max_spec) + "TAIL_BEYOND_BUDGET"
    agent = ChunkReviewAgent(llm=client)
    agent.run(_chunk_input(spec_excerpt=oversized_spec))
    assert len(client.prompts) == 1  # exactly one LLM call: the review itself
    assert "TAIL_BEYOND_BUDGET" not in client.prompts[0]
    assert "S" * 100 in client.prompts[0]
