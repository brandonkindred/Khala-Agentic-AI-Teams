"""Unit tests for ReleaseNotesAgent."""

from __future__ import annotations

from software_engineering_team.technical_writers.release_notes_agent import agent as rn_mod
from software_engineering_team.technical_writers.release_notes_agent.agent import (
    ReleaseNotesAgent,
    build_fallback_release_notes,
)
from software_engineering_team.technical_writers.release_notes_agent.models import (
    ReleaseFailure,
    ReleaseNotesInput,
    ReleaseStorySummary,
)
from software_engineering_team.tests.conftest import (
    _patch_fenced_response,
    _strands_model_double,
)


class _FakeCompleteJson:
    """Stand-in for complete_json_with_continuation used to unit-test ReleaseNotesAgent
    without exercising the real parsing/recovery logic (that's covered separately in
    test_shared_llm.py)."""

    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc
        self.calls = []

    def __call__(self, model, prompt, *, system_prompt=None, **kwargs):
        self.calls.append(prompt)
        if self._raise:
            raise self._raise
        return self._payload or {}


def _build_agent(monkeypatch, fake):
    monkeypatch.setattr(rn_mod, "complete_json_with_continuation", fake)
    return ReleaseNotesAgent(llm_client=_strands_model_double())


def _sample_input() -> ReleaseNotesInput:
    return ReleaseNotesInput(
        version="v1.2.0",
        sprint_name="Sprint 7",
        sprint_id="sprint-7",
        shipped_at_iso="2026-07-14T00:00:00Z",
        stories=[
            ReleaseStorySummary(
                id="story-1",
                title="Add dark mode",
                user_story="As a user I want dark mode so my eyes don't hurt.",
                acceptance_criteria=["Toggle persists across sessions"],
            )
        ],
    )


def test_release_notes_run_happy_path(monkeypatch) -> None:
    fake = _FakeCompleteJson(
        {
            "markdown": "# Release v1.2.0\n\n## Highlights\n\n- Dark mode\n",
            "summary": "Dark mode shipped.",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(_sample_input())
    assert out.llm_failed is False
    assert out.error is None
    assert out.markdown.startswith("# Release v1.2.0")
    assert out.summary == "Dark mode shipped."
    assert len(fake.calls) == 1


def test_release_notes_run_llm_exception_falls_back(monkeypatch) -> None:
    fake = _FakeCompleteJson(raise_exc=RuntimeError("boom"))
    a = _build_agent(monkeypatch, fake)
    out = a.run(_sample_input())
    assert out.llm_failed is True
    assert out.error == "boom"
    assert "# Release v1.2.0" in out.markdown
    assert "Add dark mode" in out.markdown


def test_release_notes_run_recovers_fenced_json_response(monkeypatch) -> None:
    """End-to-end (no complete_json_with_continuation mocking): a markdown-fenced
    LLM response is recovered instead of crashing on a bare json.loads, exercising
    the real extract_json_from_response fallback through the shared helper."""
    payload = {
        "markdown": "# Release v1.2.0\n\n## Highlights\n\n- Dark mode\n",
        "summary": "fenced ok",
    }
    _patch_fenced_response(monkeypatch, payload)
    a = ReleaseNotesAgent(llm_client=_strands_model_double())
    out = a.run(_sample_input())
    assert out.llm_failed is False
    assert out.markdown == payload["markdown"]
    assert out.summary == "fenced ok"


def test_release_notes_run_empty_markdown_falls_back(monkeypatch) -> None:
    """A parseable-but-empty ``markdown`` field is treated as a degraded response,
    not a crash — the deterministic fallback still fires."""
    fake = _FakeCompleteJson({"markdown": "", "summary": "nothing to say"})
    a = _build_agent(monkeypatch, fake)
    out = a.run(_sample_input())
    assert out.llm_failed is True
    assert out.error == "empty markdown from LLM"
    assert "# Release v1.2.0" in out.markdown


def test_build_fallback_release_notes_renders_failures_and_empty_stories() -> None:
    """With no stories and a mix of failure severities, the deterministic
    fallback: (1) renders the "no stories recorded"/"None." placeholders,
    (2) lists every failure under Known Issues, and (3) seeds Next-sprint
    candidates from only the critical/high failures."""
    input_data = ReleaseNotesInput(
        version="v1.3.0",
        sprint_name="Sprint 8",
        sprint_id="sprint-8",
        failures=[
            ReleaseFailure(
                source="qa",
                severity="critical",
                summary="Checkout crashes on empty cart",
                location="checkout.py:42",
                recommendation="Guard against empty cart before submit",
            ),
            ReleaseFailure(source="devops", severity="low", summary="Flaky log noise"),
        ],
    )
    out = build_fallback_release_notes(input_data, "no LLM available")
    assert out.llm_failed is True
    assert out.error == "no LLM available"
    assert "_No stories recorded for this sprint._" in out.markdown
    assert "_None._" in out.markdown
    assert "[critical] Checkout crashes on empty cart (qa: checkout.py:42)" in out.markdown
    assert "[low] Flaky log noise" in out.markdown
    assert "Guard against empty cart before submit" in out.markdown


def test_resolve_model_lazily_imports_and_caches_default_model() -> None:
    """With no llm_client injected, _resolve_model() lazily resolves (and
    caches) the shared ``documentation`` Strands model on first use."""
    agent = ReleaseNotesAgent()
    assert agent._model_resolved is False
    resolved = agent._resolve_model()
    assert resolved is not None
    assert agent._model_resolved is True
    assert agent._resolve_model() is resolved
